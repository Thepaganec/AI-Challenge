import sys
sys.dont_write_bytecode = True

import asyncio
import json
import os
import re
import traceback
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, List, Tuple

from dotenv import load_dotenv
load_dotenv(override=True)

from core.api.gptmodel import GPTModel
from core.agent.agent_logger import AgentFileLogger
from core.agent.memory_store import AgentMemoryStore
from core.agent.profile_store import AgentProfileStore
from core.agent.task_state_store import TaskStateStore
from core.agent.strategies import (
    build_sliding_window,
    parse_facts_and_strip_user_text,
    build_facts_strategy,
    build_summary_strategy,
)


class LLMAgentServer:
    """
    Локальный TCP сервер (JSONL протокол).
    Клиент шлёт одну строку JSON -> сервер отвечает либо одной строкой, либо стримом (chunk/done).

    В рамках Day 10:
    - Sliding Window
    - Sticky Facts (key-value facts) + последние N сообщений
    - Branching (ветки от checkpoint)
    """

    # === Инициализация сервера ===

    # Создаёт хранилища сессий/профилей, GPT-клиент, кэш тарифов и таблицу роутинга action->handler для JSONL протокола.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        api_key_env: str = "PROXYAPI_KEY",
        base_url: str = "https://openai.api.proxyapi.ru/v1",
        timeout_sec: int = 60,
    ):
        self.host = host
        self.port = port

        self.base_dir = os.path.dirname(__file__)
        self.project_root = os.path.abspath(os.path.join(self.base_dir, "..", ".."))
        self.logger = AgentFileLogger(logs_dir=self.base_dir, prefix="agentlogs")
        self.logger.cleanup_old_logs(keep_days=3)

        self.memory_dir = os.path.join(self.base_dir, "memory")
        self.memory_store = AgentMemoryStore(base_dir=self.memory_dir)
        self.profile_store = AgentProfileStore(file_path=os.path.join(self.base_dir, "profiles.json"))
        self.task_store = TaskStateStore(file_path=os.path.join(self.base_dir, "task_state.json"))
        self._last_task_state_log_sig: str = ""

        self.gpt = GPTModel(api_key_env=api_key_env, base_url=base_url, timeout_sec=timeout_sec, logger=self.logger)

        self.pricing_cache: Dict[str, Dict[str, float]] = {}
        self._action_handlers: Dict[str, Callable[[Dict[str, Any], asyncio.StreamWriter], Awaitable[None]]] = {
            "ping": self._handle_ping,
            "list_sessions": self._handle_list_sessions,
            "get_session": self._handle_get_session,
            "reset_session": self._handle_reset_session,
            "list_branches": self._handle_list_branches,
            "set_active_branch": self._handle_switch_branch,
            "switch_branch": self._handle_switch_branch,
            "list_checkpoints": self._handle_list_checkpoints,
            "create_checkpoint": self._handle_create_checkpoint,
            "create_branch": self._handle_create_branch,
            "get_memory": self._handle_get_memory,
            "save_memory": self._handle_save_memory,
            "list_profiles": self._handle_list_profiles,
            "get_profile": self._handle_get_profile,
            "save_profile": self._handle_save_profile,
            "delete_profile": self._handle_delete_profile,
            "set_active_profile": self._handle_set_active_profile,
            "get_profile_state": self._handle_get_profile_state,
            "get_task_state": self._handle_get_task_state,
            "generate_task_plan": self._handle_generate_task_plan,
            "confirm_task_plan": self._handle_confirm_task_plan,
            "pause_task": self._handle_pause_task,
            "resume_task": self._handle_resume_task,
            "next_task_step": self._handle_next_task_step,
            "update_task_progress": self._handle_update_task_progress,
            "delete_task": self._handle_delete_task,
            "stream_chat": self._handle_stream_chat,
        }
        self._model_context_limit: Dict[str, int] = {
            "gpt-3.5-turbo": 16384,
            "gpt-4o-mini": 128000,
            "gpt-4o": 128000,
            "gpt-5.2-chat-latest": 400000,
        }

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def preload_pricing(self) -> None:
        try:
            self.logger.write("INFO", "Загрузка тарифов ProxyAPI (pricing/list)...")
            self.pricing_cache = await self.gpt.get_pricing_rub_per_1m()
            self.logger.write("SUCCESS", "Тарифы загружены", extra=f"models={len(self.pricing_cache)}")
        except Exception as e:
            self.logger.write("WARN", "Не удалось загрузить тарифы ProxyAPI", extra=str(e))
            self.pricing_cache = {}

    # === Транспорт JSONL ===

    # Отправляет ответы клиенту обычной строкой или по частям, а также унифицирует отправку сообщений об ошибке.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _send_json(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)
        await writer.drain()

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _send_json_maybe_chunked(self, writer: asyncio.StreamWriter, payload: Dict[str, Any], *, max_line_bytes: int = 60000) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        raw = (text + "\n").encode("utf-8")

        if len(raw) <= max_line_bytes:
            writer.write(raw)
            await writer.drain()
            return

        part_size = max(1000, max_line_bytes - 2000)
        parts = [text[i:i + part_size] for i in range(0, len(text), part_size)]

        start = {"type": "chunked_start", "orig_type": payload.get("type"), "chunks": len(parts)}
        writer.write((json.dumps(start, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        for i, part in enumerate(parts):
            msg = {"type": "chunked_part", "orig_type": payload.get("type"), "i": i, "data": part}
            writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()

        end = {"type": "chunked_end", "orig_type": payload.get("type")}
        writer.write((json.dumps(end, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _send_error(self, writer: asyncio.StreamWriter, message: str) -> None:
        await self._send_json(writer, {"type": "error", "message": message})

    async def _send_task_signal(
        self,
        writer: asyncio.StreamWriter,
        *,
        message: str,
        stage: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "type": "task_signal",
            "message": str(message or "").strip(),
            "stage": str(stage or "").strip(),
        }
        if isinstance(extra, dict) and extra:
            payload["extra"] = extra
        await self._send_json(writer, payload)

    # === Работа с ветками и чекпоинтами ===

    # Нормализует структуру ветки, подготавливает историю и память, создаёт чекпоинты и новые ветки от среза диалога.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _get_branch(self, session: Dict[str, Any], branch_id: Optional[str]) -> Tuple[str, Dict[str, Any]]:
        branches = session.get("branches") or {}
        if not isinstance(branches, dict):
            branches = {}
            session["branches"] = branches

        active = (session.get("active_branch") or "main").strip() or "main"
        if branch_id:
            bid = str(branch_id).strip()
        else:
            bid = active

        if bid not in branches:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            branches[bid] = {
                "branch_id": bid,
                "name": bid,
                "created_at": now,
                "updated_at": now,
                "facts": {},
                "checkpoints": [],   # <<< ВАЖНО: список, не dict
                "history": [],
                "summary": "",
                "memory_layers": {
                    "short_term": [],
                    "working": {},
                    "long_term": {},
                },
            }

        session["active_branch"] = bid
        self._ensure_branch_memory_model(branches[bid])
        return bid, branches[bid]

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _branch_history(self, branch: Dict[str, Any]) -> List[Dict[str, str]]:
        h = branch.get("history")
        if not isinstance(h, list):
            h = []
            branch["history"] = h

        normalized: List[Dict[str, str]] = []
        for m in h:
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "").strip()
            content = m.get("content")
            if role and content is not None:
                normalized.append({"role": role, "content": str(content)})

        branch["history"] = normalized
        return branch["history"]

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _make_checkpoint(self, history: List[Dict[str, str]], name: Optional[str] = None) -> Dict[str, Any]:
        cut = 0
        try:
            cut = int(len(history) if isinstance(history, list) else 0)
        except Exception:
            cut = 0

        cp_id = str(uuid.uuid4())
        cp_name = (name or "").strip()
        if not cp_name:
            cp_name = f"checkpoint_{cut}"

        return {
            "id": cp_id,
            "name": cp_name,
            "cut": cut,  # индекс "после последнего сообщения"
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _create_branch_from_checkpoint(self, session: Dict[str, Any], base_branch_id: str, checkpoint: Dict[str, Any], name: Optional[str] = None) -> str:
        branches = session.get("branches")
        if not isinstance(branches, dict):
            branches = {}
            session["branches"] = branches

        base_branch = branches.get(base_branch_id) if isinstance(branches.get(base_branch_id), dict) else None
        if base_branch is None:
            base_branch = branches.get("main") if isinstance(branches.get("main"), dict) else {"title": "main", "history": [], "facts": {}, "checkpoints": []}
            branches[base_branch_id] = base_branch

        base_history = base_branch.get("history") if isinstance(base_branch.get("history"), list) else []
        try:
            cut = int(checkpoint.get("cut") or 0)
        except Exception:
            cut = 0

        new_history = list(base_history[:cut])

        new_id = str(uuid.uuid4())[:8]
        new_title = (name or "").strip() or f"branch_{new_id}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        branches[new_id] = {
            "title": new_title,
            "history": new_history,
            "facts": dict(base_branch.get("facts") or {}) if isinstance(base_branch.get("facts"), dict) else {},
            "checkpoints": [],
            "summary": str(base_branch.get("summary") or ""),
            "memory_layers": self._copy_memory_layers(base_branch.get("memory_layers")),
            "created_at": now,
            "updated_at": now,
        }

        session["branches"] = branches
        return new_id

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _copy_memory_layers(self, layers: Any) -> Dict[str, Any]:
        if not isinstance(layers, dict):
            return {"short_term": [], "working": {}, "long_term": {}}
        short_term = layers.get("short_term")
        working = layers.get("working")
        long_term = layers.get("long_term")
        return {
            "short_term": list(short_term) if isinstance(short_term, list) else [],
            "working": dict(working) if isinstance(working, dict) else {},
            "long_term": dict(long_term) if isinstance(long_term, dict) else {},
        }

    # Проверяет обязательные инварианты структуры данных и при необходимости достраивает недостающие поля до корректного состояния.

    def _ensure_branch_memory_model(self, branch: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(branch.get("summary"), str):
            branch["summary"] = ""
        branch["memory_layers"] = self._copy_memory_layers(branch.get("memory_layers"))
        return branch["memory_layers"]

    # Проверяет обязательные инварианты структуры данных и при необходимости достраивает недостающие поля до корректного состояния.

    def _ensure_title(self, session: Dict[str, Any], user_text: str) -> None:
        self.memory_store.set_title_if_empty(session, user_text)

    # === Подготовка контекста и метрик ===

    # Собирает системный контекст (memory/profile/summary), оценивает токены, пишет структурные логи API и считает стоимость ответа.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _estimate_tokens_text(self, text: str) -> int:
        clean = (text or "").strip()
        if not clean:
            return 0
        return max(1, int(len(clean) / 4))

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _estimate_tokens_messages(self, messages: List[Dict[str, str]]) -> int:
        total = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            total += self._estimate_tokens_text(str(msg.get("content") or ""))
            total += 4
        return int(total)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _resolve_context_limit(self, model: str) -> int:
        model_id = (model or "").strip()
        return int(self._model_context_limit.get(model_id, 128000))

    # Собирает производные данные/текст из текущего состояния, чтобы использовать их как часть контекста или итогового ответа.

    def _build_memory_system_text(self, memory_layers: Dict[str, Any]) -> Optional[str]:
        if not isinstance(memory_layers, dict):
            return None
        working = memory_layers.get("working")
        long_term = memory_layers.get("long_term")
        lines: List[str] = []
        if isinstance(working, dict) and working:
            lines.append("WORKING MEMORY:")
            for k, v in working.items():
                if k and v is not None:
                    lines.append(f"- {k}: {v}")
        if isinstance(long_term, dict) and long_term:
            lines.append("LONG-TERM MEMORY:")
            for k, v in long_term.items():
                if k and v is not None:
                    lines.append(f"- {k}: {v}")
        if not lines:
            return None
        return "\n".join(lines)

    # Собирает производные данные/текст из текущего состояния, чтобы использовать их как часть контекста или итогового ответа.

    def _build_profile_system_text(self, profile_name: str, profile_description: str) -> Optional[str]:
        clean_name = str(profile_name or "").strip()
        clean_desc = str(profile_description or "").strip()
        if not clean_name or not clean_desc:
            return None
        return (
            "USER PROFILE:\n"
            f"- name: {clean_name}\n"
            f"- description: {clean_desc}\n"
            "Follow this profile automatically when generating the answer."
        )

    def _build_task_system_text(self, task_state: Dict[str, Any]) -> Optional[str]:
        if not isinstance(task_state, dict):
            return None
        if bool(task_state.get("is_paused")):
            return None
        task = str(task_state.get("task") or "").strip()
        plan = task_state.get("plan") if isinstance(task_state.get("plan"), list) else []
        done = task_state.get("done") if isinstance(task_state.get("done"), list) else []
        state = str(task_state.get("state") or "planning").strip().lower()
        step = int(task_state.get("step") or 0)
        total = int(task_state.get("total") or len(plan))
        current = str(task_state.get("current") or "").strip()
        expected_action = str(task_state.get("expected_action") or "").strip()

        if state == "done":
            return None
        if not task or total <= 0 or not plan:
            return None

        plan_text = "; ".join(str(x).strip() for x in plan if str(x).strip()) or "-"
        done_text = "; ".join(str(x).strip() for x in done if str(x).strip()) or "-"

        return (
            "[TASK]\n"
            f"task: {task}\n"
            f"state: {state}\n"
            f"step: {step}/{total}\n"
            f"current: {current or '-'}\n"
            f"expected_action: {expected_action or '-'}\n"
            f"plan: {plan_text}\n"
            f"done: {done_text}\n\n"
            "Rules:\n"
            "- Work only inside the current step.\n"
            "- Do not skip FSM stages.\n"
            "- If step is complete, proceed via next_step.\n"
            "- Never claim a state transition unless server task action was executed."
        )

    def _make_auto_plan(self, task_text: str) -> List[str]:
        clean_task = " ".join(str(task_text or "").strip().split())
        if not clean_task:
            clean_task = "Текущая задача"
        return [
            f"Уточнить требования и критерии приемки: {clean_task}",
            "Реализовать решение и подготовить артефакты.",
            "Провести проверку результата и исправить замечания.",
            "Зафиксировать итог и подготовить завершение задачи.",
        ]

    def _normalize_plan_item_text(self, value: Any) -> str:
        clean = str(value or "").strip()
        if not clean:
            return ""
        clean = re.sub(r"^\d+[\).\-\s]+", "", clean).strip("- ").strip()
        return clean

    def _is_plan_confirmation_signal(self, user_text: str) -> bool:
        txt = " ".join(str(user_text or "").strip().lower().split())
        if not txt:
            return False
        txt = txt.replace("ё", "е")
        patterns = (
            r"^да$",
            r"^ок$",
            r"^окей$",
            r"^все ок$",
            r"^все окей$",
            r"^все хорошо$",
            r"^все верно$",
            r"^все правильно$",
            r"^подтверждаю$",
            r"^подтверждаю план$",
            r"^согласен$",
            r"^согласна$",
            r"^согласен с планом$",
            r"^согласна с планом$",
            r"^да, подтверждаю$",
            r"^ок, подтверждаю$",
            r"^можно$",
            r"^начина(й|ем)",
            r"^можно начинать",
            r"^старт",
            r"^поехали",
            r"^выполняй план$",
            r"^выполни план$",
            r"^приступай$",
            r"^приступай к выполнению$",
            r"^начинай выполнение$",
        )
        return any(re.search(p, txt) for p in patterns)

    def _is_validation_failed_signal(self, assistant_text: str) -> bool:
        txt = " ".join(str(assistant_text or "").strip().lower().split())
        if not txt:
            return False
        bad_markers = (
            "не прошло",
            "есть замечания",
            "есть проблемы",
            "нужно доработ",
            "требуется доработ",
            "ошибк",
            "несоответств",
        )
        return any(m in txt for m in bad_markers)

    def _is_plan_reject_signal(self, user_text: str) -> bool:
        txt = " ".join(str(user_text or "").strip().lower().split())
        if not txt:
            return False
        txt = txt.replace("ё", "е")
        patterns = (
            r"^нет$",
            r"^не соглас",
            r"^не подходит",
            r"^отклон",
            r"^переделай",
            r"^измени план",
            r"^план не",
        )
        return any(re.search(p, txt) for p in patterns)

    def _has_step_done_marker(self, assistant_text: str) -> bool:
        txt = str(assistant_text or "")
        return "[STEP_DONE]" in txt

    def _has_validation_pass_marker(self, assistant_text: str) -> bool:
        txt = str(assistant_text or "")
        return "[VALIDATION_OK]" in txt

    def _has_validation_fail_marker(self, assistant_text: str) -> bool:
        txt = str(assistant_text or "")
        return "[VALIDATION_NEEDS_WORK]" in txt

    def _strip_control_markers(self, assistant_text: str) -> str:
        txt = str(assistant_text or "")
        for marker in ("[STEP_DONE]", "[VALIDATION_OK]", "[VALIDATION_NEEDS_WORK]"):
            txt = txt.replace(marker, "")
        return txt.strip()

    async def _llm_generate_text(
        self,
        *,
        user_text: str,
        system_text: Optional[str],
        model: str,
        endpoint: str,
        temperature: Optional[float],
        max_tokens: int,
        trace_id: str,
    ) -> Tuple[str, Dict[str, Any]]:
        text = ""
        gen = None
        try:
            gen = self.gpt.stream_chat(
                user_text=user_text,
                system_text=system_text,
                history=[],
                max_tokens=max_tokens,
                model=model,
                endpoint=endpoint,
                temperature=temperature,
                include_usage=True,
                trace_id=trace_id,
            )
            async for chunk in gen:
                text += chunk
        finally:
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass
        usage = getattr(self.gpt, "last_usage", None) or {}
        try:
            self.logger.write(
                "INFO",
                "TASK_LLM_RESPONSE",
                extra=json.dumps(
                    {
                        "req_id": trace_id,
                        "text": str(text or "")[:8000],
                        "usage": usage if isinstance(usage, dict) else {},
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return text.strip(), usage

    def _extract_json_block(self, text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        candidates: List[str] = [raw]
        fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, flags=re.IGNORECASE)
        if fence_match:
            candidates.append(fence_match.group(1).strip())
        brace_match = re.search(r"(\{[\s\S]*\})", raw)
        if brace_match:
            candidates.append(brace_match.group(1).strip())
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        return None

    def _extract_plan_items(self, plan_text: str) -> List[str]:
        obj = self._extract_json_block(plan_text)
        if isinstance(obj, dict) and isinstance(obj.get("plan"), list):
            plan = [self._normalize_plan_item_text(x) for x in obj.get("plan")]
            plan = [x for x in plan if x]
            if plan:
                return plan
        lines = [x.strip() for x in str(plan_text or "").splitlines() if x.strip()]
        plan: List[str] = []
        for line in lines:
            clean = self._normalize_plan_item_text(line)
            if len(clean) >= 3:
                plan.append(clean)
        unique: List[str] = []
        for item in plan:
            if item not in unique:
                unique.append(item)
        if unique:
            return unique[:8]
        return self._make_auto_plan(plan_text)

    def _extract_commands(self, text: str) -> List[str]:
        raw = str(text or "").strip()
        if not raw:
            return []

        obj = self._extract_json_block(raw)
        if isinstance(obj, dict):
            commands_raw = obj.get("commands")
            if isinstance(commands_raw, list):
                out = [str(x).strip() for x in commands_raw if str(x).strip()]
                if out:
                    return out
            command_raw = str(obj.get("command") or "").strip()
            if command_raw:
                return [command_raw]
            script_raw = str(obj.get("script") or "").strip()
            if script_raw:
                return [script_raw]

        # Иногда модель возвращает JSON-массив без обертки объекта.
        array_match = re.search(r"(\[[\s\S]*\])", raw)
        if array_match:
            candidate = array_match.group(1).strip()
            try:
                arr = json.loads(candidate)
                if isinstance(arr, list):
                    out = [str(x).strip() for x in arr if str(x).strip()]
                    if out:
                        return out
            except Exception:
                pass

        # Попытка вытащить commands: [ ... ] даже если остальной JSON сломан.
        commands_field = re.search(r'(?is)"commands"\s*:\s*\[(.*?)\]', raw)
        if commands_field:
            payload = commands_field.group(1)
            cmd_strings = re.findall(r'"([^"]+)"|\'([^\']+)\'', payload)
            out: List[str] = []
            for a, b in cmd_strings:
                c = (a or b or "").strip()
                if c:
                    out.append(c)
            if out:
                return out

        block_match = re.search(r"```(?:powershell|cmd|bat|shell|sh|json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
        if block_match:
            block = block_match.group(1).strip()
            if block:
                lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
                cleaned = [re.sub(r"^[-*]\s+", "", ln).strip() for ln in lines]
                out = [ln for ln in cleaned if ln]
                if out:
                    return out

        # Fallback: извлекаем "похожие на команды" строки из обычного текста.
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        out: List[str] = []
        cmd_prefix = re.compile(
            r"^(?i)(new-item|set-content|add-content|test-path|get-childitem|mkdir|md|ni|copy-item|move-item|remove-item|write-output|echo|set-location|cd|if\s*\()"
        )
        for ln in lines:
            ln = re.sub(r"^\d+[\).\s-]+", "", ln).strip()
            ln = re.sub(r"^[-*]\s+", "", ln).strip()
            if not ln:
                continue
            if cmd_prefix.match(ln):
                out.append(ln)
        if out:
            return out
        return []

    def _normalize_powershell_command(self, command: str) -> str:
        cmd = str(command or "").strip()
        if not cmd:
            return ""

        def _quote_path_value(v: str) -> str:
            value = str(v or "").strip()
            if not value:
                return value
            if (
                (value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))
            ):
                return value
            # Не оборачиваем явные выражения PowerShell.
            if value.startswith("$") or value.startswith("("):
                return value
            if " " in value:
                return "'" + value.replace("'", "''") + "'"
            return value

        # Нормализация значений для -Path/-LiteralPath (особенно для путей с пробелами).
        def _normalize_path_flags(src: str) -> str:
            # Берем значение флага до следующего именованного параметра или конца строки.
            # Это предотвращает кейс, когда `-Force` ошибочно попадал в путь.
            pattern = r"(?i)(-(?:Path|LiteralPath)\s+)([^\s'\"].*?)(?=\s+-[A-Za-z]\w*\b|$)"

            def _repl(m: re.Match) -> str:
                prefix = m.group(1)
                raw_val = m.group(2)
                return f"{prefix}{_quote_path_value(raw_val)}"

            return re.sub(pattern, _repl, src)

        cmd = _normalize_path_flags(cmd)

        # `cd <path>` -> Set-Location -LiteralPath '<path>'
        m_cd = re.match(r"^cd\s+(.+)$", cmd, flags=re.IGNORECASE)
        if m_cd:
            raw_path = str(m_cd.group(1) or "").strip()
            if raw_path:
                if (
                    (raw_path.startswith('"') and raw_path.endswith('"'))
                    or (raw_path.startswith("'") and raw_path.endswith("'"))
                ):
                    return f"Set-Location -LiteralPath {raw_path}"
                esc = raw_path.replace("'", "''")
                return f"Set-Location -LiteralPath '{esc}'"

        # `Set-Location -Path <path>` / `Set-Location -LiteralPath <path>` / `Set-Location <path>`
        m_set = re.match(
            r"^set-location(?:\s+-(?:path|literalpath))?\s+(.+)$",
            cmd,
            flags=re.IGNORECASE,
        )
        if m_set:
            raw_path = str(m_set.group(1) or "").strip()
            if raw_path:
                if (
                    (raw_path.startswith('"') and raw_path.endswith('"'))
                    or (raw_path.startswith("'") and raw_path.endswith("'"))
                ):
                    return f"Set-Location -LiteralPath {raw_path}"
                esc = raw_path.replace("'", "''")
                return f"Set-Location -LiteralPath '{esc}'"
        # Делаем New-Item более идемпотентным.
        if re.match(r"^\s*new-item\b", cmd, flags=re.IGNORECASE) and (" -Force" not in cmd and " -force" not in cmd):
            cmd = f"{cmd} -Force"
        return cmd

    def _is_navigation_only_command(self, command: str) -> bool:
        cmd = str(command or "").strip().lower()
        if not cmd:
            return False
        return bool(
            re.match(r"^(cd|set-location)\b", cmd)
            and "|" not in cmd
            and ";" not in cmd
            and "&&" not in cmd
            and "||" not in cmd
        )

    async def _run_powershell_commands(
        self,
        commands: List[str],
        *,
        timeout_sec: int = 120,
        on_signal: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        trace_id: str = "",
        stage: str = "",
    ) -> Dict[str, Any]:
        runs: List[Dict[str, Any]] = []
        all_ok = True
        for idx, command in enumerate(commands, start=1):
            cmd = self._normalize_powershell_command(command)
            if not cmd:
                continue
            try:
                self.logger.write(
                    "INFO",
                    "TASK_COMMAND_START",
                    extra=json.dumps(
                        {
                            "req_id": trace_id,
                            "stage": stage,
                            "index": idx,
                            "command": cmd,
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass
            if on_signal is not None:
                try:
                    await on_signal({"event": "command_start", "index": idx, "command": cmd})
                except Exception:
                    pass
            if self._is_navigation_only_command(cmd):
                runs.append(
                    {
                        "index": idx,
                        "command": cmd,
                        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "ok": True,
                    }
                )
                try:
                    self.logger.write(
                        "INFO",
                        "TASK_COMMAND_DONE",
                        extra=json.dumps(
                            {
                                "req_id": trace_id,
                                "stage": stage,
                                "index": idx,
                                "command": cmd,
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                                "navigation_noop": True,
                            },
                            ensure_ascii=False,
                        ),
                    )
                except Exception:
                    pass
                if on_signal is not None:
                    try:
                        await on_signal({"event": "command_done", "index": idx, "command": cmd, "exit_code": 0})
                    except Exception:
                        pass
                continue
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                cmd,
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                await proc.communicate()
                stdout_b, stderr_b = b"", b"Command timed out"
            code = int(proc.returncode or 0)
            if timed_out:
                code = -1
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()
            combined_error = f"{stdout}\n{stderr}".lower()
            benign_already_exists = (
                "already exists" in combined_error
                or "уже существует" in combined_error
            )
            if code != 0 and benign_already_exists:
                code = 0
                stderr = ""
            if code != 0:
                all_ok = False
            runs.append(
                {
                    "index": idx,
                    "command": cmd,
                    "started_at": started,
                    "exit_code": code,
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                    "ok": code == 0,
                }
            )
            try:
                self.logger.write(
                    "INFO" if code == 0 else "WARN",
                    "TASK_COMMAND_DONE",
                    extra=json.dumps(
                        {
                            "req_id": trace_id,
                            "stage": stage,
                            "index": idx,
                            "command": cmd,
                            "exit_code": code,
                            "stdout": stdout[-1200:],
                            "stderr": stderr[-1200:],
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass
            if on_signal is not None:
                try:
                    await on_signal({"event": "command_done", "index": idx, "command": cmd, "exit_code": code})
                except Exception:
                    pass
        return {"ok": all_ok and bool(runs), "runs": runs}

    def _task_log_snapshot(self, task_state: Dict[str, Any]) -> Dict[str, Any]:
        state = str(task_state.get("state") or "planning")
        step = int(task_state.get("step") or 0)
        total = int(task_state.get("total") or 0)
        paused = bool(task_state.get("is_paused", False))
        current = str(task_state.get("current") or "").strip()
        return {
            "state": state,
            "step": step,
            "total": total,
            "paused": paused,
            "current": current,
        }

    def _log_task_action(
        self,
        *,
        action: str,
        before: Dict[str, Any],
        after: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        payload: Dict[str, Any] = {
            "action": action,
            "before": self._task_log_snapshot(before if isinstance(before, dict) else {}),
        }
        if isinstance(after, dict):
            payload["after"] = self._task_log_snapshot(after)
            payload["transition"] = f"{payload['before']['state']} -> {payload['after']['state']}"
        if error:
            payload["error"] = error
        self.logger.write("INFO" if not error else "WARN", "TASK_FSM_ACTION", extra=json.dumps(payload, ensure_ascii=False))

    def _log_task_payload(self, *, name: str, req_id: str, payload: Any) -> None:
        try:
            self.logger.write(
                "INFO",
                name,
                extra=json.dumps({"req_id": req_id, "payload": payload}, ensure_ascii=False),
            )
        except Exception:
            pass

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _merge_system_text(self, *chunks: Optional[str]) -> Optional[str]:
        parts = [str(c).strip() for c in chunks if isinstance(c, str) and str(c).strip()]
        if not parts:
            return None
        return "\n\n".join(parts)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _stringify_history_for_summary(self, messages: List[Dict[str, str]]) -> str:
        lines: List[str] = []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").strip().lower()
            content = str(m.get("content") or "").strip()
            if not role or not content:
                continue
            lines.append(f"{role}: {content}")
        return "\n".join(lines).strip()

    # Сжимает старую часть истории через отдельный LLM-вызов, чтобы сохранить долгосрочный контекст и уменьшить объём токенов в рабочих запросах.

    async def _summarize_history_with_llm(
        self,
        *,
        older_history: List[Dict[str, str]],
        model: str,
        endpoint: str,
        temperature: Optional[float],
        max_tokens: int,
        trace_id: str,
    ) -> str:
        transcript = self._stringify_history_for_summary(older_history)
        if not transcript:
            return ""

        summary_system = (
            "You summarize dialogue history in Russian.\n"
            "Keep only durable facts, constraints, decisions, and open tasks.\n"
            "Do not include greetings, filler, or repeated details.\n"
            "Output compact bullet points."
        )

        parts: List[str] = []
        gen = None
        try:
            gen = self.gpt.stream_chat(
                user_text=f"Суммаризуй историю диалога:\n\n{transcript}",
                system_text=summary_system,
                history=[],
                max_tokens=max_tokens,
                model=model,
                endpoint=endpoint,
                temperature=temperature,
                include_usage=False,
                trace_id=f"{trace_id}-summary",
            )
            async for chunk in gen:
                if chunk:
                    parts.append(chunk)
        finally:
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass
        return "".join(parts).strip()

    # Сохраняет или фиксирует данные в целевом хранилище с базовой валидацией входных параметров.

    def _save_memory_item(self, memory_layers: Dict[str, Any], layer: str, key: str, value: str) -> bool:
        layer_key = (layer or "").strip().lower()
        memory_layers = memory_layers if isinstance(memory_layers, dict) else {}
        if layer_key == "short_term":
            short_term = memory_layers.get("short_term")
            if not isinstance(short_term, list):
                short_term = []
            short_term.append(
                {
                    "key": key or "note",
                    "value": value,
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            if len(short_term) > 50:
                short_term = short_term[-50:]
            memory_layers["short_term"] = short_term
            return True
        if layer_key in ("working", "long_term"):
            bucket = memory_layers.get(layer_key)
            if not isinstance(bucket, dict):
                bucket = {}
            clean_key = (key or "").strip() or "note"
            bucket[clean_key] = value
            memory_layers[layer_key] = bucket
            return True
        return False

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _sync_short_term_from_history(self, memory_layers: Dict[str, Any], history: List[Dict[str, str]], max_items: int = 20) -> None:
        if not isinstance(memory_layers, dict):
            return
        synced: List[Dict[str, str]] = []
        for msg in history[-max_items:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "").strip()
            if role and content:
                synced.append({"key": role, "value": content})
        memory_layers["short_term"] = synced

    # Формирует расширенный SERVER_API_REQUEST лог с оценкой токенов, режимом контекста и параметрами памяти для последующего анализа качества запросов.

    def _log_api_request(
        self,
        *,
        req_id: str,
        session_id: str,
        branch_id: str,
        model: str,
        endpoint: str,
        max_tokens: int,
        temperature: Optional[float],
        keep_last_n: int,
        strategy: str,
        user_text: str,
        history_for_llm: List[Dict[str, str]],
        system_text: Optional[str],
        explicit_memory: Any,
    ) -> None:
        try:
            context_tokens_est = self._estimate_tokens_messages(history_for_llm) + self._estimate_tokens_text(system_text or "")
            user_tokens_est = self._estimate_tokens_text(user_text)
            payload: Dict[str, Any] = {
                "req_id": req_id,
                "session_id": session_id,
                "branch_id": branch_id,
                "model": model,
                "endpoint": endpoint,
                "max_tokens": int(max_tokens),
                "temperature": temperature,
                "keep_last_n": int(keep_last_n),
                "context_strategy": strategy,
                "history_messages": int(len(history_for_llm)),
                "system_text_len": int(len(system_text or "")),
                "user_text_len": int(len(user_text or "")),
                "context_tokens_est": int(context_tokens_est),
                "user_tokens_est": int(user_tokens_est),
                "include_usage": True,
            }
            if isinstance(explicit_memory, dict):
                payload["memory_write"] = {
                    "layer": str(explicit_memory.get("layer") or ""),
                    "has_key": bool(str(explicit_memory.get("key") or "").strip()),
                    "value_len": int(len(str(explicit_memory.get("value") or ""))),
                }
            payload["source"] = "SERVER"
            self.logger.write("INFO", "SERVER_API_REQUEST", extra=json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as e:
            self.logger.write("WARN", "Не удалось сформировать API_REQUEST лог", extra=str(e))

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _calc_cost_rub(self, model_id: str, usage: Dict[str, Any]) -> Optional[float]:
        try:
            price = self.pricing_cache.get((model_id or "").strip())
            if not isinstance(price, dict):
                return None

            prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0

            return (
                (float(prompt_tokens) / 1_000_000.0) * float(price.get("in", 0))
                + (float(completion_tokens) / 1_000_000.0) * float(price.get("out", 0))
            )
        except Exception:
            return None

    # === Обработчики команд клиента ===

    # Реализует действия протокола: сессии, ветки, checkpoints, память, профили и основной chat-stream с сохранением результатов в стор.

    # Обрабатывает действие 'ping' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_ping(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "pong"})

    # Обрабатывает действие 'list_sessions' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_list_sessions(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        sessions = self.memory_store.list_sessions()
        await self._send_json(
            writer,
            {
                "type": "sessions",
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "title": s.title,
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                    }
                    for s in sessions
                ],
            },
        )

    # Обрабатывает действие 'get_session' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_get_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        branches = session.get("branches")
        if isinstance(branches, dict):
            for _, branch in branches.items():
                if isinstance(branch, dict):
                    self._ensure_branch_memory_model(branch)
        await self._send_json_maybe_chunked(writer, {"type": "session", "session": session})

    # Обрабатывает действие 'reset_session' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_reset_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        self.memory_store.delete_session_file(session_id)
        await self._send_json(writer, {"type": "ok"})

    # Обрабатывает действие 'list_branches' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_list_branches(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return

        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        active = session.get("active_branch") if isinstance(session.get("active_branch"), str) else "main"

        out = []
        for bid, b in branches.items():
            if not isinstance(b, dict):
                continue
            out.append({"id": bid, "title": b.get("title") or b.get("name") or bid})

        await self._send_json(writer, {"type": "branches", "branches": out, "active_branch": active})

    # Обрабатывает действие 'switch_branch' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_switch_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip()
        if not session_id or not branch_id:
            await self._send_error(writer, "session_id and branch_id are required")
            return

        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        if branch_id not in branches:
            await self._send_error(writer, "branch not found")
            return

        session["active_branch"] = branch_id
        session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "ok", "ok": True, "active_branch": branch_id})

    # Обрабатывает действие 'list_checkpoints' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_list_checkpoints(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip() or None
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return

        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        active = session.get("active_branch") if isinstance(session.get("active_branch"), str) else "main"
        bid = branch_id or active
        if bid not in branches:
            bid = "main"

        branch = branches.get(bid) if isinstance(branches.get(bid), dict) else {}
        cps = branch.get("checkpoints")
        if not isinstance(cps, list):
            cps = []
            branch["checkpoints"] = cps

        await self._send_json(writer, {"type": "checkpoints", "checkpoints": cps, "active_branch": bid})

    # Обрабатывает действие 'create_checkpoint' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_create_checkpoint(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip() or None
        name = (request.get("name") or "").strip() or None
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return

        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        session["branches"] = branches

        active = session.get("active_branch") if isinstance(session.get("active_branch"), str) else "main"
        bid = branch_id or active
        if bid not in branches:
            bid, branch = self._get_branch(session, bid)
            branches[bid] = branch

        branch = branches.get(bid) if isinstance(branches.get(bid), dict) else {}
        history = self._branch_history(branch)

        cps = branch.get("checkpoints")
        if not isinstance(cps, list):
            cps = []
            branch["checkpoints"] = cps

        cp = self._make_checkpoint(history, name=name)
        cps.append(cp)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        branch["updated_at"] = now
        session["updated_at"] = now
        session["active_branch"] = bid
        branches[bid] = branch
        session["branches"] = branches
        self.memory_store.save_session(session)

        await self._send_json(
            writer,
            {"type": "ok", "ok": True, "checkpoint_id": cp.get("id"), "checkpoint": cp, "active_branch": bid},
        )

    # Обрабатывает действие 'create_branch' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_create_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        from_branch_id = (request.get("from_branch_id") or request.get("branch_id") or "").strip() or None
        checkpoint_id = (request.get("checkpoint_id") or "").strip()
        new_branch_name = (request.get("new_branch_name") or request.get("name") or "").strip() or None

        if not session_id or not checkpoint_id:
            await self._send_error(writer, "session_id and checkpoint_id are required")
            return

        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        session["branches"] = branches

        active = session.get("active_branch") if isinstance(session.get("active_branch"), str) else "main"
        base_bid = (from_branch_id or active).strip() or "main"
        if base_bid not in branches:
            base_bid, base_branch = self._get_branch(session, base_bid)
            branches[base_bid] = base_branch

        base_branch = branches.get(base_bid) if isinstance(branches.get(base_bid), dict) else {}
        cps = base_branch.get("checkpoints")
        if not isinstance(cps, list):
            cps = []
            base_branch["checkpoints"] = cps

        cp = None
        for c in cps:
            if isinstance(c, dict) and str(c.get("id")) == checkpoint_id:
                cp = c
                break

        if not cp:
            await self._send_error(writer, "checkpoint not found")
            return

        new_bid = self._create_branch_from_checkpoint(session, base_bid, cp, name=new_branch_name)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = now
        session["active_branch"] = new_bid
        self.memory_store.save_session(session)

        await self._send_json(writer, {"type": "ok", "ok": True, "branch_id": new_bid, "active_branch": new_bid})

    # Обрабатывает действие 'get_memory' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_get_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip() or None
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return

        session = self.memory_store.load_session(session_id)
        bid, branch = self._get_branch(session, branch_id)
        memory_layers = self._ensure_branch_memory_model(branch)

        await self._send_json(
            writer,
            {
                "type": "memory",
                "session_id": session_id,
                "active_branch": bid,
                "memory_layers": memory_layers,
            },
        )

    # Обрабатывает действие 'save_memory' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_save_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip() or None
        layer = (request.get("layer") or "").strip()
        key = str(request.get("key") or "").strip()
        value = str(request.get("value") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        if not layer or not value:
            await self._send_error(writer, "layer and value are required")
            return

        session = self.memory_store.load_session(session_id)
        bid, branch = self._get_branch(session, branch_id)
        memory_layers = self._ensure_branch_memory_model(branch)
        ok = self._save_memory_item(memory_layers, layer, key, value)
        if not ok:
            await self._send_error(writer, "layer must be one of: short_term, working, long_term")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        branch["memory_layers"] = memory_layers
        branch["updated_at"] = now
        session["branches"][bid] = branch
        session["active_branch"] = bid
        session["updated_at"] = now
        self.memory_store.save_session(session)

        await self._send_json(
            writer,
            {
                "type": "ok",
                "ok": True,
                "active_branch": bid,
                "memory_layers": memory_layers,
            },
        )

    # Обрабатывает действие 'list_profiles' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_list_profiles(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "profiles",
                "profiles": state.get("available_profiles") or [],
                "active_profile": state.get("active_profile") or "",
            },
        )

    # Обрабатывает действие 'get_profile' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_get_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        if not profile_name:
            await self._send_error(writer, "profile_name is required")
            return
        profile = self.profile_store.get_profile(profile_name)
        if not isinstance(profile, dict):
            await self._send_error(writer, "profile not found")
            return
        await self._send_json(writer, {"type": "profile", "profile": profile})

    # Обрабатывает действие 'save_profile' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_save_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        description = str(request.get("description") or "")
        if not profile_name:
            await self._send_error(writer, "profile_name is required")
            return
        try:
            self.profile_store.save_profile(profile_name, description)
            self.profile_store.set_active_profile(profile_name)
            state = self.profile_store.get_state()
            await self._send_json(
                writer,
                {
                    "type": "ok",
                    "ok": True,
                    "active_profile": state.get("active_profile") or "",
                    "profiles": state.get("available_profiles") or [],
                },
            )
        except Exception as e:
            await self._send_error(writer, str(e))

    # Обрабатывает действие 'delete_profile' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_delete_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        if not profile_name:
            await self._send_error(writer, "profile_name is required")
            return
        self.profile_store.delete_profile(profile_name)
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "ok",
                "ok": True,
                "active_profile": state.get("active_profile") or "",
                "profiles": state.get("available_profiles") or [],
            },
        )

    # Обрабатывает действие 'set_active_profile' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_set_active_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        self.profile_store.set_active_profile(profile_name)
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "ok",
                "ok": True,
                "active_profile": state.get("active_profile") or "",
                "profiles": state.get("available_profiles") or [],
            },
        )

    # Обрабатывает действие 'get_profile_state' из входящего JSON-запроса, валидирует параметры и формирует структурированный ответ клиенту.

    async def _handle_get_profile_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "profile_state",
                "active_profile": state.get("active_profile") or "",
                "profiles": state.get("available_profiles") or [],
            },
        )

    async def _handle_get_task_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        task_state = self.task_store.get_state()
        try:
            sig = json.dumps(self._task_log_snapshot(task_state), ensure_ascii=False, sort_keys=True)
        except Exception:
            sig = ""
        if sig and sig != self._last_task_state_log_sig:
            self._last_task_state_log_sig = sig
            self._log_task_action(action="get_task_state", before=task_state, after=task_state)
        await self._send_json(writer, {"type": "task_state", "task_state": task_state})

    async def _handle_generate_task_plan(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        task = str(request.get("task") or "").strip()
        if not task:
            await self._send_error(writer, "task is required")
            return
        before = self.task_store.get_state()
        try:
            plan = self._make_auto_plan(task)
            task_state = self.task_store.generate_plan(task=task, plan=plan)
            self._log_task_action(action="generate_task_plan", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_GENERATE_PLAN_FAILED", extra=str(e))
            self._log_task_action(action="generate_task_plan", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_confirm_task_plan(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.confirm_plan()
            self._log_task_action(action="confirm_task_plan", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_CONFIRM_PLAN_FAILED", extra=str(e))
            self._log_task_action(action="confirm_task_plan", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_pause_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.set_paused(True)
            self._log_task_action(action="pause_task", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_PAUSE_FAILED", extra=str(e))
            self._log_task_action(action="pause_task", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_resume_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.set_paused(False)
            self._log_task_action(action="resume_task", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_RESUME_FAILED", extra=str(e))
            self._log_task_action(action="resume_task", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_next_task_step(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.next_step()
            self._log_task_action(action="next_task_step", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_NEXT_STEP_FAILED", extra=str(e))
            self._log_task_action(action="next_task_step", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_update_task_progress(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        current = str(request.get("current") or "")
        expected_action = str(request.get("expected_action") or "")
        done_item = str(request.get("done_item") or "")
        step_raw = request.get("step")
        step = None
        if step_raw is not None:
            try:
                step = int(step_raw)
            except Exception:
                step = None
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.update_progress(
                current=current,
                expected_action=expected_action,
                done_item=done_item,
                step=step,
            )
            self._log_task_action(action="update_task_progress", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_UPDATE_PROGRESS_FAILED", extra=str(e))
            self._log_task_action(action="update_task_progress", before=before, error=str(e))
            await self._send_error(writer, str(e))

    async def _handle_delete_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        before = self.task_store.get_state()
        try:
            task_state = self.task_store.clear_task()
            self._log_task_action(action="delete_task", before=before, after=task_state)
            await self._send_json(writer, {"type": "task_state", "task_state": task_state})
        except Exception as e:
            self.logger.write("WARN", "TASK_DELETE_FAILED", extra=str(e))
            self._log_task_action(action="delete_task", before=before, error=str(e))
            await self._send_error(writer, str(e))

    # Основной сценарий запроса: читает параметры стратегии, готовит контекст, запускает стрим к модели, обновляет ветку/память и возвращает done с полной телеметрией.

    async def _handle_stream_chat(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        req_id = str(uuid.uuid4())[:8]
        user_text = (request.get("user_text") or "").strip()
        session_id = (request.get("session_id") or "").strip()
        branch_id = (request.get("branch_id") or "").strip() or None

        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        if not user_text:
            await self._send_error(writer, "Empty user_text")
            return

        model = (request.get("model") or "").strip() or self.gpt.model
        endpoint = (request.get("endpoint") or "chat").strip() or "chat"
        max_tokens = int(request.get("max_tokens") or 512)

        temperature = request.get("temperature", None)
        if temperature is not None:
            try:
                temperature = float(temperature)
            except Exception:
                temperature = None

        try:
            keep_last_n = int(request.get("keep_last_n") or 10)
        except Exception:
            keep_last_n = 10
        keep_last_n = max(1, keep_last_n)

        summary_cfg = request.get("summary_config") if isinstance(request.get("summary_config"), dict) else {}
        summary_model = str(summary_cfg.get("model") or model).strip() or model
        summary_endpoint = str(summary_cfg.get("endpoint") or endpoint).strip() or endpoint
        summary_max_tokens = int(summary_cfg.get("max_tokens") or 600)
        summary_max_tokens = max(32, summary_max_tokens)
        summary_temperature = summary_cfg.get("temperature", temperature)
        if summary_temperature is not None:
            try:
                summary_temperature = float(summary_temperature)
            except Exception:
                summary_temperature = None

        strategy = (request.get("context_strategy") or "sliding").strip().lower()
        use_profile = bool(request.get("use_profile", False))

        strategy_for_context = strategy if strategy in ("sliding", "facts", "summary", "branching") else "sliding"
        strategy_display = strategy_for_context

        session = self.memory_store.load_session(session_id)
        self._ensure_title(session, user_text)
        session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.memory_store.save_session(session)
        title = str(session.get("title") or "")

        async def _send_orchestrator_done(
            *,
            answer_text: str,
            usage_data: Dict[str, Any],
            final_task_state: Dict[str, Any],
            strategy_name: str = "task_orchestrator",
            error_text: str = "",
        ) -> None:
            if answer_text.strip():
                await self._send_json(writer, {"type": "chunk", "chunk": answer_text.strip() + "\n"})
            task_state_now = final_task_state if isinstance(final_task_state, dict) else self.task_store.get_state()
            prompt_tokens = int(usage_data.get("prompt_tokens") or usage_data.get("input_tokens") or 0)
            completion_tokens = int(usage_data.get("completion_tokens") or usage_data.get("output_tokens") or 0)
            total_tokens_call = int(usage_data.get("total_tokens") or (prompt_tokens + completion_tokens))
            token_stats = {
                "user_text_tokens_est": int(self._estimate_tokens_text(user_text)),
                "context_tokens_est": 0,
                "assistant_tokens": int(completion_tokens),
                "total_tokens_call": int(total_tokens_call),
                "dialog_tokens_est": 0,
                "model_context_limit": int(self._resolve_context_limit(model)),
                "may_exceed_context": False,
            }
            message_stats = {
                "strategy": strategy_name,
                "branch_id": "main",
                "use_profile": False,
                "active_profile": "Без профиля",
                "profile_description_len": 0,
                "profile_applied": False,
                "keep_last_n": int(keep_last_n),
                "sent_messages": 1,
                "facts_count": None,
                "memory_layers_counts": {"short_term": 0, "working": 0, "long_term": 0},
                "task_state": str(task_state_now.get("state") or "planning"),
                "task_step": int(task_state_now.get("step") or 0),
                "task_total": int(task_state_now.get("total") or 0),
                "task_paused": bool(task_state_now.get("is_paused", False)),
                "task_injected": True,
                "token_stats": token_stats,
            }
            if error_text:
                message_stats["error"] = error_text
            await self._send_json(
                writer,
                {
                    "type": "done",
                    "model": model,
                    "endpoint": endpoint,
                    "usage": usage_data,
                    "cost_rub": self._calc_cost_rub(model_id=model, usage=usage_data),
                    "session_id": session_id,
                    "title": title,
                    "active_branch": "main",
                    "message_stats": message_stats,
                    "facts": {},
                    "memory_layers": {},
                    "token_stats": token_stats,
                    "task_state": task_state_now,
                    "profile_info": {
                        "use_profile": False,
                        "active_profile": "",
                        "profile_description_len": 0,
                        "profile_applied": False,
                    },
                },
            )
            try:
                await self._send_task_signal(writer, message="Ожидание следующего действия", stage="idle")
            except Exception:
                pass

        async def _signal(stage: str, message: str, **extra: Any) -> None:
            payload = {k: v for k, v in extra.items() if v is not None}
            await self._send_task_signal(writer, message=message, stage=stage, extra=payload if payload else None)

        task_state = self.task_store.get_state()
        if bool(task_state.get("is_paused", False)):
            before_resume = task_state
            try:
                task_state = self.task_store.set_paused(False)
                self._log_task_action(action="auto_resume_on_message", before=before_resume, after=task_state)
            except Exception as e:
                self._log_task_action(action="auto_resume_on_message", before=before_resume, error=str(e))
                task_state = self.task_store.get_state()

        state_now = str(task_state.get("state") or "planning").strip().lower()
        has_task = bool(str(task_state.get("task") or "").strip()) and int(task_state.get("total") or 0) > 0

        # Шаг 1-3: новая задача -> LLM планирование -> показать план на подтверждение.
        if (not has_task) or state_now == "done":
            await _signal("planning", "Формирую план задачи через LLM")
            plan_prompt_system = (
                "Ты task-orchestrator. Составь короткий, исполнимый план в 3-6 шагов.\n"
                "Не добавляй отдельные шаги навигации по директориям (например, 'перейти в папку').\n"
                "Ответ строго JSON: {\"plan\": [\"шаг 1\", \"шаг 2\", \"...\"]}\n"
                "Без markdown и пояснений."
            )
            plan_raw, usage = await self._llm_generate_text(
                user_text=f"Задача пользователя: {user_text}",
                system_text=plan_prompt_system,
                model=model,
                endpoint=endpoint,
                temperature=temperature,
                max_tokens=max_tokens,
                trace_id=f"{req_id}-plan",
            )
            plan = self._extract_plan_items(plan_raw)
            self._log_task_payload(name="TASK_PLAN_PARSED", req_id=f"{req_id}-plan", payload=plan)
            before_generate = self.task_store.get_state()
            task_state = self.task_store.generate_plan(task=user_text, plan=plan)
            self._log_task_action(action="auto_generate_task_plan_llm", before=before_generate, after=task_state)
            await _signal("planning", "План сформирован, ожидается подтверждение пользователя", steps=len(plan))
            plan_lines = "\n".join(f"{i}. {step}" for i, step in enumerate(plan, start=1))
            answer = (
                "План сформирован. Подтверди план сообщением (например: \"да\"), "
                "или отклони и дай правки.\n\n"
                f"[PLAN]\n{plan_lines}"
            )
            await _send_orchestrator_done(answer_text=answer, usage_data=usage, final_task_state=task_state, strategy_name="task_plan")
            return

        # Шаг 4: planning -> approve/reject (reject = replanning loop).
        if state_now == "planning":
            if self._is_plan_confirmation_signal(user_text):
                before_confirm = task_state
                task_state = self.task_store.confirm_plan()
                self._log_task_action(action="auto_confirm_plan_on_message", before=before_confirm, after=task_state)
                await _signal("execution", "План подтвержден, начинаю выполнение")
                state_now = "execution"
            else:
                await _signal("planning", "Обновляю план по комментарию пользователя")
                task_text = str(task_state.get("task") or "").strip() or user_text
                replan_system = (
                    "Ты task-orchestrator. Перепланируй задачу с учетом комментария пользователя.\n"
                    "Не добавляй отдельные шаги навигации по директориям (например, 'перейти в папку').\n"
                    "Ответ строго JSON: {\"plan\": [\"шаг 1\", \"шаг 2\", \"...\"]}\n"
                    "Без markdown и пояснений."
                )
                replan_raw, usage = await self._llm_generate_text(
                    user_text=(
                        f"Исходная задача: {task_text}\n"
                        f"Комментарий пользователя к плану: {user_text}"
                    ),
                    system_text=replan_system,
                    model=model,
                    endpoint=endpoint,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    trace_id=f"{req_id}-replan",
                )
                new_plan = self._extract_plan_items(replan_raw)
                self._log_task_payload(name="TASK_REPLAN_PARSED", req_id=f"{req_id}-replan", payload=new_plan)
                before_replan = task_state
                task_state = self.task_store.generate_plan(task=task_text, plan=new_plan)
                log_action = "auto_replan_reject_signal" if self._is_plan_reject_signal(user_text) else "auto_replan_feedback"
                self._log_task_action(action=log_action, before=before_replan, after=task_state)
                await _signal("planning", "План обновлен, ожидается подтверждение", steps=len(new_plan))
                plan_lines = "\n".join(f"{i}. {step}" for i, step in enumerate(new_plan, start=1))
                answer = (
                    "План обновлен по твоему комментарию. Подтверди его или снова отклони.\n\n"
                    f"[PLAN]\n{plan_lines}"
                )
                await _send_orchestrator_done(answer_text=answer, usage_data=usage, final_task_state=task_state, strategy_name="task_replan")
                return

        # Шаги 5-6: execution + validation под управлением агента (полный проход за один цикл).
        if state_now in ("execution", "validation"):
            task_text = str(task_state.get("task") or "").strip()
            usage_agg: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            progress_lines: List[str] = []
            last_run_result: Dict[str, Any] = {}

            def _acc_usage(usage_piece: Dict[str, Any]) -> None:
                usage_agg["prompt_tokens"] += int(usage_piece.get("prompt_tokens") or usage_piece.get("input_tokens") or 0)
                usage_agg["completion_tokens"] += int(usage_piece.get("completion_tokens") or usage_piece.get("output_tokens") or 0)
                usage_agg["total_tokens"] += int(
                    usage_piece.get("total_tokens")
                    or (
                        int(usage_piece.get("prompt_tokens") or usage_piece.get("input_tokens") or 0)
                        + int(usage_piece.get("completion_tokens") or usage_piece.get("output_tokens") or 0)
                    )
                )

            def _format_runs_error(title: str, run_data: Dict[str, Any]) -> str:
                lines = [title]
                runs = run_data.get("runs") if isinstance(run_data.get("runs"), list) else []
                for item in runs:
                    if not isinstance(item, dict):
                        continue
                    lines.append(f"- cmd: {item.get('command')}")
                    lines.append(f"  exit: {item.get('exit_code')}")
                    stderr = str(item.get("stderr") or "").strip()
                    if stderr:
                        lines.append(f"  stderr: {stderr[:500]}")
                return "\n".join(lines)

            exec_system = (
                "Ты исполнитель задач для локального Windows проекта.\n"
                "Сформируй команды, которые агент должен выполнить автоматически для ТЕКУЩЕГО шага.\n"
                "Не добавляй команды смены директории (`cd`, `Set-Location`) без необходимости: агент уже запускает всё из корня проекта.\n"
                "Команды должны быть идемпотентными: повторный запуск не должен падать (используй `-Force`, `Test-Path`, `if (...) { ... }`).\n"
                "В execution НЕ делай финальную проверку результата задачи и не проверяй артефакты будущих шагов.\n"
                "Ответ строго JSON: {\"commands\": [\"powershell command 1\", \"command 2\"], \"note\": \"...\"}\n"
                "Только безопасные и конкретные команды без поясняющего текста вне JSON."
            )
            validation_system = (
                "Ты проверяющий агент. По результатам выполнения дай команды проверки.\n"
                "Не добавляй команды смены директории (`cd`, `Set-Location`) без необходимости: агент уже запускает всё из корня проекта.\n"
                "Команды проверки должны быть идемпотентными и явно проверять целевой результат задачи.\n"
                "Ответ строго JSON: {\"commands\": [\"powershell command 1\", \"command 2\"], \"success_criteria\": \"...\"}\n"
                "Без markdown и пояснений вне JSON."
            )

            safety_limit = max(3, int(task_state.get("total") or 0) + 3)
            for _ in range(safety_limit):
                cur_state = str(task_state.get("state") or "").strip().lower()
                if cur_state == "execution":
                    plan = task_state.get("plan") if isinstance(task_state.get("plan"), list) else []
                    done_steps = task_state.get("done") if isinstance(task_state.get("done"), list) else []
                    current_step = str(task_state.get("current") or "").strip()
                    exec_user = (
                        f"Задача: {task_text}\n"
                        f"План: {json.dumps(plan, ensure_ascii=False)}\n"
                        f"Выполнено: {json.dumps(done_steps, ensure_ascii=False)}\n"
                        f"Текущий шаг: {current_step}\n"
                        f"Рабочая директория: {self.project_root}"
                    )
                    exec_raw, usage_exec = await self._llm_generate_text(
                        user_text=exec_user,
                        system_text=exec_system,
                        model=model,
                        endpoint=endpoint,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        trace_id=f"{req_id}-exec-plan",
                    )
                    _acc_usage(usage_exec)
                    commands = self._extract_commands(exec_raw)
                    self._log_task_payload(name="TASK_EXEC_COMMANDS_PARSED", req_id=f"{req_id}-exec-plan", payload=commands)
                    if not commands:
                        step_lc = current_step.lower()
                        if (
                            "перейти" in step_lc
                            or "директори" in step_lc
                            or "папк" in step_lc
                            or "root" in step_lc
                            or "корнев" in step_lc
                        ):
                            commands = ["Write-Output 'navigation no-op: cwd already set by agent'"]
                        else:
                            self.logger.write(
                                "WARN",
                                "TASK_EXEC_EMPTY_COMMANDS_RAW",
                                extra=exec_raw[:800],
                            )
                    if not commands:
                        err_state = self.task_store.update_progress(
                            expected_action="Ошибка: LLM не вернул исполняемые команды. Ожидается уточнение пользователя."
                        )
                        await _send_orchestrator_done(
                            answer_text="Ошибка оркестратора: не удалось получить команды выполнения от LLM.",
                            usage_data=usage_agg,
                            final_task_state=err_state,
                            strategy_name="task_execution_failed",
                            error_text="empty_execution_commands",
                        )
                        return

                    await _signal("execution", f"Выполняю шаг: {current_step}", step=task_state.get("step"), total=task_state.get("total"))

                    async def _on_exec_signal(payload: Dict[str, Any]) -> None:
                        event = str(payload.get("event") or "")
                        idx = payload.get("index")
                        cmd = str(payload.get("command") or "")
                        exit_code = payload.get("exit_code")
                        if event == "command_start":
                            await _signal("execution", f"Команда {idx}: {cmd}", command_index=idx)
                        elif event == "command_done":
                            await _signal("execution", f"Команда {idx} завершена (code={exit_code})", command_index=idx, exit_code=exit_code)

                    run_result = await self._run_powershell_commands(
                        commands,
                        on_signal=_on_exec_signal,
                        trace_id=f"{req_id}-exec-run",
                        stage="execution",
                    )
                    last_run_result = run_result
                    if not bool(run_result.get("ok", False)):
                        await _signal("execution", "Шаг завершился ошибкой, запрашиваю корректировку команд")
                        repair_system = (
                            "Ты исправляешь неудачный execution-шаг.\n"
                            "На входе: текущий шаг, команды и ошибка выполнения.\n"
                            "Верни только исправленные команды для ЭТОГО шага.\n"
                            "Формат строго JSON: {\"commands\": [\"...\"]}"
                        )
                        repair_user = (
                            f"Задача: {task_text}\n"
                            f"Текущий шаг: {current_step}\n"
                            f"Команды: {json.dumps(commands, ensure_ascii=False)}\n"
                            f"Результат ошибки: {json.dumps(run_result, ensure_ascii=False)}\n"
                            f"Рабочая директория: {self.project_root}"
                        )
                        repair_raw, usage_repair = await self._llm_generate_text(
                            user_text=repair_user,
                            system_text=repair_system,
                            model=model,
                            endpoint=endpoint,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            trace_id=f"{req_id}-exec-repair",
                        )
                        _acc_usage(usage_repair)
                        repair_commands = self._extract_commands(repair_raw)
                        self._log_task_payload(name="TASK_EXEC_REPAIR_COMMANDS_PARSED", req_id=f"{req_id}-exec-repair", payload=repair_commands)
                        if repair_commands:
                            await _signal("execution", "Выполняю исправленные команды шага")
                            repair_result = await self._run_powershell_commands(
                                repair_commands,
                                on_signal=_on_exec_signal,
                                trace_id=f"{req_id}-exec-repair-run",
                                stage="execution_repair",
                            )
                            last_run_result = repair_result
                            if bool(repair_result.get("ok", False)):
                                run_result = repair_result
                            else:
                                run_result = repair_result
                        if not bool(run_result.get("ok", False)):
                            fail_state = self.task_store.update_progress(
                                expected_action="Выполнение шага завершилось ошибкой. Нужна корректировка команды/плана."
                            )
                            await _send_orchestrator_done(
                                answer_text=_format_runs_error("Ошибка выполнения шага. Логи:", run_result),
                                usage_data=usage_agg,
                                final_task_state=fail_state,
                                strategy_name="task_execution_failed",
                                error_text="command_execution_failed",
                            )
                            return

                    progress_lines.append(f"Шаг выполнен: {current_step}")
                    before_next = self.task_store.get_state()
                    task_state = self.task_store.next_step()
                    self._log_task_action(action="auto_advance_execution_after_run", before=before_next, after=task_state)
                    continue

                if cur_state == "validation":
                    await _signal("validation", "Запрашиваю команды проверки результата")
                    plan = task_state.get("plan") if isinstance(task_state.get("plan"), list) else []
                    validation_user = (
                        f"Задача: {task_text}\n"
                        f"План: {json.dumps(plan, ensure_ascii=False)}\n"
                        f"Результаты выполнения: {json.dumps(last_run_result, ensure_ascii=False)}\n"
                        f"Рабочая директория: {self.project_root}"
                    )
                    val_raw, usage_val = await self._llm_generate_text(
                        user_text=validation_user,
                        system_text=validation_system,
                        model=model,
                        endpoint=endpoint,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        trace_id=f"{req_id}-validate-plan",
                    )
                    _acc_usage(usage_val)
                    validation_commands = self._extract_commands(val_raw)
                    self._log_task_payload(name="TASK_VALIDATION_COMMANDS_PARSED", req_id=f"{req_id}-validate-plan", payload=validation_commands)
                    if not validation_commands:
                        fail_state = self.task_store.update_progress(
                            expected_action="Ошибка: LLM не вернул команды проверки. Требуется повтор валидации."
                        )
                        await _send_orchestrator_done(
                            answer_text="Ошибка валидации: LLM не вернул команды проверки результата.",
                            usage_data=usage_agg,
                            final_task_state=fail_state,
                            strategy_name="task_validation_failed",
                            error_text="empty_validation_commands",
                        )
                        return

                    async def _on_val_signal(payload: Dict[str, Any]) -> None:
                        event = str(payload.get("event") or "")
                        idx = payload.get("index")
                        cmd = str(payload.get("command") or "")
                        exit_code = payload.get("exit_code")
                        if event == "command_start":
                            await _signal("validation", f"Проверка {idx}: {cmd}", command_index=idx)
                        elif event == "command_done":
                            await _signal("validation", f"Проверка {idx} завершена (code={exit_code})", command_index=idx, exit_code=exit_code)

                    validation_result = await self._run_powershell_commands(
                        validation_commands,
                        on_signal=_on_val_signal,
                        trace_id=f"{req_id}-validation-run",
                        stage="validation",
                    )
                    if bool(validation_result.get("ok", False)):
                        before_done = task_state
                        task_state = self.task_store.transition("done")
                        self._log_task_action(action="auto_validation_to_done", before=before_done, after=task_state)
                        done_snapshot = task_state
                        task_state = self.task_store.clear_task()
                        self._log_task_action(action="auto_clear_done_task", before=done_snapshot, after=task_state)
                        answer_lines = progress_lines + ["Проверка пройдена. Задача завершена и автоматически удалена."]
                        await _send_orchestrator_done(
                            answer_text="\n".join(answer_lines),
                            usage_data=usage_agg,
                            final_task_state=task_state,
                            strategy_name="task_validation_done",
                        )
                        return

                    before_back = task_state
                    task_state = self.task_store.transition("execution")
                    task_state = self.task_store.update_progress(
                        expected_action="Проверка не пройдена. Агент сообщил об ошибке, требуется доработка шага."
                    )
                    self._log_task_action(action="auto_validation_to_execution", before=before_back, after=task_state)
                    answer_lines = progress_lines + [_format_runs_error("Проверка не пройдена. Логи:", validation_result)]
                    await _send_orchestrator_done(
                        answer_text="\n".join(answer_lines),
                        usage_data=usage_agg,
                        final_task_state=task_state,
                        strategy_name="task_validation_failed",
                        error_text="validation_failed",
                    )
                    return

                if cur_state == "done":
                    await _send_orchestrator_done(
                        answer_text="Задача уже завершена.",
                        usage_data=usage_agg,
                        final_task_state=task_state,
                        strategy_name="task_done",
                    )
                    return

                break

            fail_safe_state = self.task_store.update_progress(
                expected_action="Оркестратор прервал цикл выполнения по safety-limit. Требуется диагностика."
            )
            await _send_orchestrator_done(
                answer_text="Оркестратор остановлен по safety-limit. Проверь логи.",
                usage_data=usage_agg,
                final_task_state=fail_safe_state,
                strategy_name="task_execution_failed",
                error_text="safety_limit_reached",
            )
            return

        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        session["branches"] = branches

        if strategy_for_context == "branching":
            active = session.get("active_branch") if isinstance(session.get("active_branch"), str) else "main"
            bid = (branch_id or active).strip() or "main"
        else:
            bid = "main"

        if bid not in branches:
            bid, branch = self._get_branch(session, bid)
            branches[bid] = branch

        session["active_branch"] = bid

        branch = branches.get(bid) if isinstance(branches.get(bid), dict) else {"title": bid, "history": [], "facts": {}, "checkpoints": []}
        history = self._branch_history(branch)
        memory_layers = self._ensure_branch_memory_model(branch)

        user_text_for_api = user_text
        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        if strategy_for_context == "facts":
            facts, cleaned_user_text = parse_facts_and_strip_user_text(user_text=user_text, prev_facts=facts)
            branch["facts"] = facts
            user_text_for_api = cleaned_user_text or "Учти обновленные факты и продолжай."

        system_text = None
        history_for_llm: List[Dict[str, str]] = []

        if strategy_for_context == "facts":
            system_text, history_for_llm = build_facts_strategy(history, facts, keep_last_n)
        elif strategy_for_context == "summary":
            older_history = history[:-keep_last_n] if len(history) > keep_last_n else []
            tail_history = history[-keep_last_n:] if keep_last_n > 0 else []
            summary_text = ""
            if older_history:
                try:
                    summary_text = await self._summarize_history_with_llm(
                        older_history=older_history,
                        model=summary_model,
                        endpoint=summary_endpoint,
                        temperature=summary_temperature,
                        max_tokens=summary_max_tokens,
                        trace_id=req_id,
                    )
                except Exception as e:
                    self.logger.write("WARN", "SUMMARY_LLM_FAILED", extra=str(e))
                    previous_summary = str(branch.get("summary") or "")
                    fallback_system, _, fallback_summary = build_summary_strategy(
                        history=history,
                        keep_last_n=keep_last_n,
                        previous_summary=previous_summary,
                    )
                    summary_text = str(fallback_summary or "")
                    if not summary_text and isinstance(fallback_system, str):
                        summary_text = fallback_system.replace("SUMMARY OF PREVIOUS DIALOG:\n", "", 1).strip()

            branch["summary"] = summary_text
            system_text = f"SUMMARY OF PREVIOUS DIALOG:\n{summary_text}" if summary_text else None
            history_for_llm = tail_history
        else:
            history_for_llm = build_sliding_window(history, keep_last_n)

        memory_system_text = self._build_memory_system_text(memory_layers)
        system_text = self._merge_system_text(memory_system_text, system_text)

        profile_state = self.profile_store.get_state()
        active_profile = str(profile_state.get("active_profile") or "").strip()
        profile_description = ""
        profile_applied = False
        if active_profile:
            profile = self.profile_store.get_profile(active_profile)
            if isinstance(profile, dict):
                profile_description = str(profile.get("description") or "")
        if use_profile and active_profile and profile_description.strip():
            profile_system_text = self._build_profile_system_text(active_profile, profile_description)
            system_text = self._merge_system_text(profile_system_text, system_text)
            profile_applied = True

        task_system_text = self._build_task_system_text(task_state)
        task_injected = bool(task_system_text)
        system_text = self._merge_system_text(task_system_text, system_text)

        task_state_for_call = str(task_state.get("state") or "planning")
        if task_state_for_call == "execution":
            execution_instruction = (
                "TASK ORCHESTRATOR MODE (execution):\n"
                "- Выполняй текущий шаг самостоятельно и выдай конкретный результат этого шага.\n"
                "- Не проси пользователя выполнить шаг вместо тебя.\n"
                "- Если шаг завершен в этом ответе, добавь в конце отдельной строкой маркер [STEP_DONE].\n"
                "- Если шаг не завершен, не добавляй маркер."
            )
            system_text = self._merge_system_text(execution_instruction, system_text)
        elif task_state_for_call == "validation":
            validation_instruction = (
                "TASK ORCHESTRATOR MODE (validation):\n"
                "- Проверь результат и дай короткий вердикт.\n"
                "- Если валидация успешна, добавь в конце отдельной строкой [VALIDATION_OK].\n"
                "- Если нужна доработка, добавь в конце отдельной строкой [VALIDATION_NEEDS_WORK]."
            )
            system_text = self._merge_system_text(validation_instruction, system_text)

        explicit_memory = request.get("memory_write")
        if isinstance(explicit_memory, dict):
            layer = str(explicit_memory.get("layer") or "").strip()
            key = str(explicit_memory.get("key") or "").strip()
            value = str(explicit_memory.get("value") or "").strip()
            if layer and value:
                self._save_memory_item(memory_layers, layer, key, value)
                branch["memory_layers"] = memory_layers

        history.append({"role": "user", "content": user_text})

        gen = None
        assistant_answer = ""

        try:
            self._log_api_request(
                req_id=req_id,
                session_id=session_id,
                branch_id=bid,
                model=model,
                endpoint=endpoint,
                max_tokens=max_tokens,
                temperature=temperature,
                keep_last_n=keep_last_n,
                strategy=strategy_for_context,
                user_text=user_text_for_api,
                history_for_llm=history_for_llm,
                system_text=system_text,
                explicit_memory=explicit_memory,
            )
            self.logger.write(
                "INFO",
                "SERVER_PROFILE_CONTEXT",
                extra=json.dumps(
                    {
                        "req_id": req_id,
                        "use_profile": use_profile,
                        "active_profile": active_profile,
                        "profile_description_len": len(profile_description),
                        "profile_applied": profile_applied,
                    },
                    ensure_ascii=False,
                ),
            )
            self.logger.write(
                "INFO",
                "SERVER_TASK_CONTEXT",
                extra=json.dumps(
                    {
                        "req_id": req_id,
                        "task_state": str(task_state.get("state") or "planning"),
                        "task_step": int(task_state.get("step") or 0),
                        "task_total": int(task_state.get("total") or 0),
                        "task_paused": bool(task_state.get("is_paused", False)),
                        "task_injected": task_injected,
                    },
                    ensure_ascii=False,
                ),
            )
            gen = self.gpt.stream_chat(
                user_text=user_text_for_api,
                system_text=system_text,
                history=history_for_llm,
                max_tokens=max_tokens,
                model=model,
                endpoint=endpoint,
                temperature=temperature,
                include_usage=True,
                trace_id=req_id,
            )

            async for chunk in gen:
                assistant_answer += chunk
                await self._send_json(writer, {"type": "chunk", "chunk": chunk})

            usage = getattr(self.gpt, "last_usage", None) or {}
            cost_rub = self._calc_cost_rub(model_id=model, usage=usage)

            history.append({"role": "assistant", "content": assistant_answer})
            self._sync_short_term_from_history(memory_layers, history)
            branch["memory_layers"] = memory_layers

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            branch["history"] = history
            branch["updated_at"] = now
            branches[bid] = branch
            session["branches"] = branches
            session["updated_at"] = now
            session["active_branch"] = bid
            self.memory_store.save_session(session)

            task_before_auto_step = self.task_store.get_state()
            current_task_state = task_before_auto_step
            try:
                cur_state = str(task_before_auto_step.get("state") or "planning")
                if (not bool(task_before_auto_step.get("is_paused", False))) and cur_state == "execution":
                    if self._has_step_done_marker(assistant_answer):
                        current_task_state = self.task_store.next_step()
                        self._log_task_action(
                            action="auto_advance_execution_after_response",
                            before=task_before_auto_step,
                            after=current_task_state,
                        )
                    else:
                        self._log_task_action(
                            action="auto_hold_execution_wait_step_done",
                            before=task_before_auto_step,
                            after=task_before_auto_step,
                        )
                elif (not bool(task_before_auto_step.get("is_paused", False))) and cur_state == "validation":
                    if self._has_validation_fail_marker(assistant_answer) or self._is_validation_failed_signal(assistant_answer):
                        current_task_state = self.task_store.transition("execution")
                        self._log_task_action(
                            action="auto_validation_to_execution",
                            before=task_before_auto_step,
                            after=current_task_state,
                        )
                    elif self._has_validation_pass_marker(assistant_answer):
                        current_task_state = self.task_store.transition("done")
                        self._log_task_action(
                            action="auto_validation_to_done",
                            before=task_before_auto_step,
                            after=current_task_state,
                        )
                    else:
                        self._log_task_action(
                            action="auto_hold_validation_wait_marker",
                            before=task_before_auto_step,
                            after=task_before_auto_step,
                        )
            except Exception as e:
                self._log_task_action(
                    action="auto_advance_after_response",
                    before=task_before_auto_step,
                    error=str(e),
                )
                current_task_state = self.task_store.get_state()

            assistant_answer = self._strip_control_markers(assistant_answer)
            if history and isinstance(history[-1], dict) and str(history[-1].get("role") or "") == "assistant":
                history[-1]["content"] = assistant_answer

            sent_messages = int(len(history_for_llm) + (1 if system_text else 0) + 1)
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total_tokens_call = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

            full_history_tokens_est = self._estimate_tokens_messages(history)
            context_tokens_est = self._estimate_tokens_messages(history_for_llm) + self._estimate_tokens_text(system_text or "")
            user_tokens_est = self._estimate_tokens_text(user_text_for_api)
            assistant_tokens_est = completion_tokens if completion_tokens > 0 else self._estimate_tokens_text(assistant_answer)
            model_context_limit = self._resolve_context_limit(model)
            may_exceed_context = bool((context_tokens_est + user_tokens_est) > model_context_limit)

            token_stats = {
                "user_text_tokens_est": int(user_tokens_est),
                "context_tokens_est": int(context_tokens_est),
                "assistant_tokens": int(assistant_tokens_est),
                "total_tokens_call": int(total_tokens_call),
                "dialog_tokens_est": int(full_history_tokens_est),
                "model_context_limit": int(model_context_limit),
                "may_exceed_context": may_exceed_context,
            }
            facts_count = int(len(facts) if isinstance(facts, dict) else 0) if strategy_for_context == "facts" else None
            message_stats = {
                "strategy": strategy_display,
                "branch_id": bid,
                "use_profile": use_profile,
                "active_profile": active_profile or "Без профиля",
                "profile_description_len": int(len(profile_description)),
                "profile_applied": profile_applied,
                "keep_last_n": int(keep_last_n),
                "sent_messages": int(sent_messages),
                "facts_count": facts_count,
                "memory_layers_counts": {
                    "short_term": int(len(memory_layers.get("short_term") or [])) if isinstance(memory_layers.get("short_term"), list) else 0,
                    "working": int(len(memory_layers.get("working") or {})) if isinstance(memory_layers.get("working"), dict) else 0,
                    "long_term": int(len(memory_layers.get("long_term") or {})) if isinstance(memory_layers.get("long_term"), dict) else 0,
                },
                "task_state": str(current_task_state.get("state") or "planning"),
                "task_step": int(current_task_state.get("step") or 0),
                "task_total": int(current_task_state.get("total") or 0),
                "task_paused": bool(current_task_state.get("is_paused", False)),
                "task_injected": task_injected,
                "token_stats": token_stats,
            }

            await self._send_json(
                writer,
                {
                    "type": "done",
                    "model": model,
                    "endpoint": endpoint,
                    "usage": usage,
                    "cost_rub": cost_rub,
                    "session_id": session_id,
                    "title": session.get("title") or "",
                    "active_branch": bid,
                    "message_stats": message_stats,
                    "facts": (facts if strategy_for_context == "facts" and isinstance(facts, dict) else {}),
                    "memory_layers": memory_layers,
                    "token_stats": token_stats,
                    "task_state": current_task_state,
                    "profile_info": {
                        "use_profile": use_profile,
                        "active_profile": active_profile,
                        "profile_description_len": int(len(profile_description)),
                        "profile_applied": profile_applied,
                    },
                },
            )

        finally:
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass

    # === Цикл обслуживания сокета ===

    # Читает входящий JSONL, роутит его в нужный handler и поддерживает сервер в режиме постоянного прослушивания.

    # Запускает основной рабочий цикл и управляет потоком входящих/исходящих данных в рамках текущей роли компонента.

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.logger.write("INFO", "Клиент подключился", extra=str(peer))

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    request = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    await self._send_json(writer, {"type": "error", "message": "Invalid JSON"})
                    continue

                action = (request.get("action") or "").strip()
                handler = self._action_handlers.get(action)
                if handler is None:
                    await self._send_error(writer, "Unknown action")
                    continue
                await handler(request, writer)

        except Exception as e:
            tb = traceback.format_exc()
            msg = str(e) or "Unknown error"
            self.logger.write("ERROR", "handle_client", extra=msg)
            self.logger.write("ERROR", "TRACEBACK", extra=tb)
            try:
                await self._send_json(writer, {"type": "error", "message": msg})
            except Exception:
                pass

        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.logger.write("INFO", "Клиент отключился", extra=str(peer))

    # Запускает основной рабочий цикл и управляет потоком входящих/исходящих данных в рамках текущей роли компонента.

    async def run(self) -> None:
        await self.preload_pricing()

        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.write("INFO", "Агент запущен и слушает", extra=addrs)

        async with server:
            await server.serve_forever()


async def main() -> None:
    agent = LLMAgentServer()
    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
