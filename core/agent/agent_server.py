import asyncio
import json
import os
import signal
import sys
import traceback
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from core.LLM_API.gptmodel import GPTModel
from core.agent.agent_logger import AgentFileLogger
from core.agent.invariants import build_invariants_state, build_invariants_system_text
from core.agent.memory_store import AgentMemoryStore
from core.agent.profile_store import AgentProfileStore
from core.agent.rag_retriever import RagError, RagRetriever
from core.agent.strategies import (
    build_facts_strategy,
    build_sliding_window,
    build_summary_strategy,
    parse_facts_and_strip_user_text,
)
from core.mcp import MCPClient, MCPClientError
from core.scheduler_service.contracts import scheduler_contract_examples, validate_scheduler_create_task_payload
from core.shared.schema_validation import validate_json_value

load_dotenv(override=True)


class LLMAgentServer:
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
        self.gpt = GPTModel(api_key_env=api_key_env, base_url=base_url, timeout_sec=timeout_sec, logger=self.logger)
        self.rag = RagRetriever(
            project_root=self.project_root,
            rag_dir=str(os.getenv("RAG_DIR", "RAG")).strip() or "RAG",
            ollama_url=str(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).strip() or "http://127.0.0.1:11434",
            model=str(os.getenv("RAG_EMBED_MODEL", "nomic-embed-text")).strip() or "nomic-embed-text",
            timeout_sec=max(5, int(str(os.getenv("RAG_OLLAMA_TIMEOUT_SEC", "30")).strip() or "30")),
            llm_base_url=str(os.getenv("RAG_LLM_BASE_URL", base_url)).strip() or base_url,
            llm_api_key_env=str(os.getenv("RAG_LLM_API_KEY_ENV", api_key_env)).strip() or api_key_env,
            llm_model=str(os.getenv("RAG_LLM_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini",
            llm_timeout_sec=max(5, int(str(os.getenv("RAG_LLM_TIMEOUT_SEC", "40")).strip() or "40")),
            heuristic_w_semantic=float(str(os.getenv("RAG_HEURISTIC_W_SEMANTIC", "0.8")).strip() or "0.8"),
            heuristic_w_lexical=float(str(os.getenv("RAG_HEURISTIC_W_LEXICAL", "0.2")).strip() or "0.2"),
            llm_rerank_max_chunks=max(1, int(str(os.getenv("RAG_LLM_RERANK_MAX_CHUNKS", "8")).strip() or "8")),
            llm_rerank_max_chars_per_chunk=max(200, int(str(os.getenv("RAG_LLM_RERANK_MAX_CHARS", "1200")).strip() or "1200")),
        )
        self.rag_top_k_default = max(1, int(str(os.getenv("RAG_TOP_K_DEFAULT", "4")).strip() or "4"))
        self.rag_top_k_before_default = max(1, int(str(os.getenv("RAG_TOP_K_BEFORE_DEFAULT", "10")).strip() or "10"))
        self.rag_similarity_threshold_default = float(str(os.getenv("RAG_SIMILARITY_THRESHOLD_DEFAULT", "0.5")).strip() or "0.5")
        self.rag_top_k_after_default = max(1, int(str(os.getenv("RAG_TOP_K_AFTER_DEFAULT", "5")).strip() or "5"))
        self.rag_rewrite_default = str(os.getenv("RAG_REWRITE_DEFAULT", "false")).strip().lower() in {"1", "true", "yes", "on"}
        self.rag_rerank_mode_default = str(os.getenv("RAG_RERANK_MODE_DEFAULT", "none")).strip().lower() or "none"
        self.mcp = MCPClient(logger=self.logger)
        self.scheduler_worker_process: Optional[asyncio.subprocess.Process] = None

        self.pricing_cache: Dict[str, Dict[str, float]] = {}
        self._model_context_limit: Dict[str, int] = {
            "gpt-3.5-turbo": 16384,
            "gpt-4o-mini": 128000,
            "gpt-4o": 128000,
            "gpt-5.2-chat-latest": 400000,
        }
        self._action_handlers: Dict[str, Callable[[Dict[str, Any], asyncio.StreamWriter], Awaitable[None]]] = {}
        self._action_handlers = {
            "ping": self._handle_ping,
            "list_sessions": self._handle_list_sessions,
            "get_session": self._handle_get_session,
            "reset_session": self._handle_reset_session,
            "list_branches": self._handle_list_branches,
            "switch_branch": self._handle_switch_branch,
            "set_active_branch": self._handle_switch_branch,
            "list_checkpoints": self._handle_list_checkpoints,
            "create_checkpoint": self._handle_create_checkpoint,
            "create_branch": self._handle_create_branch,
            "get_memory": self._handle_get_memory,
            "save_memory": self._handle_save_memory,
            "get_profile": self._handle_get_profile,
            "save_profile": self._handle_save_profile,
            "delete_profile": self._handle_delete_profile,
            "set_active_profile": self._handle_set_active_profile,
            "get_profile_state": self._handle_get_profile_state,
            "list_profiles": self._handle_get_profile_state,
            "get_invariants_state": self._handle_get_invariants_state,
            "save_invariant": self._handle_save_invariant,
            "set_invariant_policy": self._handle_set_invariant_policy,
            "get_task_state": self._handle_get_task_state,
            "generate_task_plan": self._handle_generate_task_plan,
            "confirm_task_plan": self._handle_confirm_task_plan,
            "pause_task": self._handle_pause_task,
            "resume_task": self._handle_resume_task,
            "next_task_step": self._handle_next_task_step,
            "update_task_progress": self._handle_update_task_progress,
            "delete_task": self._handle_delete_task,
            "mcp_status": self._handle_mcp_status,
            "mcp_list_tools": self._handle_mcp_list_tools,
            "mcp_call_tool": self._handle_mcp_call_tool,
            "stream_chat": self._handle_stream_chat,
        }

    async def _ensure_scheduler_worker(self) -> None:
        enabled = str(os.getenv("SCHEDULER_WORKER_AUTOSTART", "true")).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            self._log("INFO", "SCHEDULER_WORKER_AUTOSTART_DISABLED")
            return
        if self.scheduler_worker_process is not None and self.scheduler_worker_process.returncode is None:
            return

        worker_command = str(os.getenv("SCHEDULER_WORKER_COMMAND") or sys.executable).strip() or sys.executable
        worker_script = str(os.getenv("SCHEDULER_WORKER_SCRIPT") or os.path.join(self.project_root, "run_scheduler_worker.py")).strip()
        worker_args = [worker_script]
        self._log("INFO", "SCHEDULER_WORKER_START", {"command": worker_command, "args": worker_args})
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        self.scheduler_worker_process = await asyncio.create_subprocess_exec(
            worker_command,
            *worker_args,
            cwd=self.project_root,
            env=dict(os.environ),
            creationflags=creationflags,
        )
        await asyncio.sleep(0.5)
        if self.scheduler_worker_process.returncode is not None:
            raise RuntimeError(f"Scheduler worker exited immediately with code {self.scheduler_worker_process.returncode}")
        self._log("SUCCESS", "SCHEDULER_WORKER_STARTED", {"pid": self.scheduler_worker_process.pid})

    async def _stop_scheduler_worker(self) -> None:
        proc = self.scheduler_worker_process
        self.scheduler_worker_process = None
        if proc is None or proc.returncode is not None:
            return
        self._log("INFO", "SCHEDULER_WORKER_STOP", {"pid": proc.pid})
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        self._log("INFO", "SCHEDULER_WORKER_STOPPED", {"returncode": proc.returncode})

    async def preload_pricing(self) -> None:
        try:
            self.logger.write("INFO", "Загрузка тарифов ProxyAPI")
            self.pricing_cache = await self.gpt.get_pricing_rub_per_1m()
            self.logger.write("SUCCESS", "Тарифы загружены", extra=f"models={len(self.pricing_cache)}")
        except Exception as e:
            self.logger.write("WARN", "Не удалось загрузить тарифы ProxyAPI", extra=str(e))
            self.pricing_cache = {}

    async def _send_json(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _send_json_maybe_chunked(self, writer: asyncio.StreamWriter, payload: Dict[str, Any], *, max_line_bytes: int = 60000) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) <= max_line_bytes:
            writer.write(raw)
            await writer.drain()
            return
        text = json.dumps(payload, ensure_ascii=False)
        part_size = max(1000, max_line_bytes - 2000)
        parts = [text[i:i + part_size] for i in range(0, len(text), part_size)]
        await self._send_json(writer, {"type": "chunked_start", "orig_type": payload.get("type"), "chunks": len(parts)})
        for idx, part in enumerate(parts):
            await self._send_json(writer, {"type": "chunked_part", "orig_type": payload.get("type"), "i": idx, "data": part})
        await self._send_json(writer, {"type": "chunked_end", "orig_type": payload.get("type")})

    async def _send_error(self, writer: asyncio.StreamWriter, message: str) -> None:
        await self._send_json(writer, {"type": "error", "message": message})

    async def _send_task_signal(self, writer: asyncio.StreamWriter, stage: str, message: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"type": "task_signal", "stage": str(stage or "").strip()}
        clean_message = str(message or "").strip()
        if clean_message:
            payload["message"] = clean_message
        if isinstance(extra, dict) and extra:
            payload["extra"] = extra
        await self._send_json(writer, payload)

    def _log(self, level: str, message: str, payload: Any = None) -> None:
        if payload is None:
            self.logger.write(level, message)
            return
        if isinstance(payload, str):
            extra = payload
        else:
            extra = json.dumps(payload, ensure_ascii=False, indent=2)
        self.logger.write(level, message, extra=extra)

    def _estimate_tokens_text(self, text: Any) -> int:
        clean = str(text or "").strip()
        return max(1, int(len(clean) / 4)) if clean else 0

    def _estimate_tokens_messages(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            total += self._estimate_tokens_text(msg.get("content"))
            total += 4
        return total

    def _resolve_context_limit(self, model: str) -> int:
        return int(self._model_context_limit.get(str(model or "").strip(), 128000))

    def _calc_cost_rub(self, model_id: str, usage: Dict[str, Any]) -> float:
        row = self.pricing_cache.get(str(model_id or "").strip()) if isinstance(self.pricing_cache, dict) else None
        if not isinstance(row, dict):
            return 0.0
        prompt_tokens = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        in_price = float(row.get("in") or 0.0)
        out_price = float(row.get("out") or 0.0)
        return (prompt_tokens / 1_000_000.0) * in_price + (completion_tokens / 1_000_000.0) * out_price

    def _merge_usage(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(left or {})
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"):
            merged[key] = int(merged.get(key) or 0) + int((right or {}).get(key) or 0)
        return merged

    def _copy_memory_layers(self, layers: Any) -> Dict[str, Any]:
        if not isinstance(layers, dict):
            return {"short_term": [], "working": {}, "long_term": {}}
        return {
            "short_term": list(layers.get("short_term")) if isinstance(layers.get("short_term"), list) else [],
            "working": dict(layers.get("working")) if isinstance(layers.get("working"), dict) else {},
            "long_term": dict(layers.get("long_term")) if isinstance(layers.get("long_term"), dict) else {},
        }

    def _ensure_branch(self, session: Dict[str, Any], branch_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        session["branches"] = branches
        active = str(session.get("active_branch") or "main").strip() or "main"
        bid = str(branch_id or active).strip() or "main"
        if bid not in branches or not isinstance(branches.get(bid), dict):
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            branches[bid] = {
                "branch_id": bid,
                "title": bid,
                "history": [],
                "facts": {},
                "checkpoints": [],
                "summary": "",
                "memory_layers": {"short_term": [], "working": {}, "long_term": {}},
                "task_state": {},
                "created_at": now,
                "updated_at": now,
            }
        branch = branches[bid]
        if not isinstance(branch.get("history"), list):
            branch["history"] = []
        if not isinstance(branch.get("facts"), dict):
            branch["facts"] = {}
        if not isinstance(branch.get("checkpoints"), list):
            branch["checkpoints"] = []
        if not isinstance(branch.get("summary"), str):
            branch["summary"] = ""
        branch["memory_layers"] = self._copy_memory_layers(branch.get("memory_layers"))
        session["active_branch"] = bid
        return bid, branch

    def _make_checkpoint(self, history: List[Dict[str, Any]], name: str = "") -> Dict[str, Any]:
        cp_name = str(name or "").strip() or f"checkpoint_{len(history)}"
        return {
            "id": str(uuid.uuid4()),
            "name": cp_name,
            "cut": int(len(history)),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _create_branch_from_checkpoint(self, session: Dict[str, Any], from_branch_id: str, checkpoint_id: str, new_branch_name: str = "") -> str:
        _, source = self._ensure_branch(session, from_branch_id)
        checkpoints = source.get("checkpoints") if isinstance(source.get("checkpoints"), list) else []
        checkpoint = next((cp for cp in checkpoints if isinstance(cp, dict) and str(cp.get("id")) == str(checkpoint_id)), None)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint not found")
        cut = int(checkpoint.get("cut") or 0)
        history = source.get("history") if isinstance(source.get("history"), list) else []
        bid = str(uuid.uuid4())[:8]
        title = str(new_branch_name or "").strip() or f"branch_{bid}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["branches"][bid] = {
            "branch_id": bid,
            "title": title,
            "history": list(history[:cut]),
            "facts": dict(source.get("facts") or {}),
            "checkpoints": [],
            "summary": str(source.get("summary") or ""),
            "memory_layers": self._copy_memory_layers(source.get("memory_layers")),
            "task_state": dict(source.get("task_state") or {}),
            "created_at": now,
            "updated_at": now,
        }
        session["active_branch"] = bid
        return bid

    def _serialize_tool_result_for_history(self, payload: Dict[str, Any]) -> str:
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False)
        return str(payload)

    def _sync_short_term_from_history(self, memory_layers: Dict[str, Any], history: List[Dict[str, Any]], keep_last_items: int = 12) -> None:
        recent: List[str] = []
        for msg in history[-keep_last_items:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            content = msg.get("content")
            if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                names = [str((item.get("function") or {}).get("name") or "") for item in msg.get("tool_calls") if isinstance(item, dict)]
                if names:
                    recent.append(f"assistant_tool_calls: {', '.join(name for name in names if name)}")
                continue
            if content is None:
                continue
            text = str(content).replace("\n", " ").strip()
            if not text:
                continue
            if len(text) > 240:
                text = text[:237].rstrip() + "..."
            recent.append(f"{role}: {text}")
        memory_layers["short_term"] = recent[-keep_last_items:]

    def _save_memory_item(self, memory_layers: Dict[str, Any], layer: str, key: str, value: str) -> None:
        clean_layer = str(layer or "").strip()
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if not clean_value:
            return
        if clean_layer == "short_term":
            bucket = memory_layers.get("short_term") if isinstance(memory_layers.get("short_term"), list) else []
            bucket.append(clean_value)
            memory_layers["short_term"] = bucket[-20:]
            return
        if clean_layer in ("working", "long_term"):
            mapping = memory_layers.get(clean_layer) if isinstance(memory_layers.get(clean_layer), dict) else {}
            mapping[clean_key or f"item_{len(mapping) + 1}"] = clean_value
            memory_layers[clean_layer] = mapping

    def _build_memory_system_text(self, memory_layers: Dict[str, Any]) -> Optional[str]:
        if not isinstance(memory_layers, dict):
            return None
        lines: List[str] = []
        if isinstance(memory_layers.get("working"), dict) and memory_layers.get("working"):
            lines.append("[MEMORY_WORKING]")
            for key, value in memory_layers["working"].items():
                lines.append(f"- {key}: {value}")
        if isinstance(memory_layers.get("long_term"), dict) and memory_layers.get("long_term"):
            lines.append("[MEMORY_LONG_TERM]")
            for key, value in memory_layers["long_term"].items():
                lines.append(f"- {key}: {value}")
        if not lines:
            return None
        return "\n".join(lines)

    def _build_profile_system_text(self, profile_name: str, profile_description: str) -> Optional[str]:
        clean_name = str(profile_name or "").strip()
        clean_desc = str(profile_description or "").strip()
        if not clean_name or not clean_desc:
            return None
        return f"[PROFILE]\n- name: {clean_name}\n- description: {clean_desc}"

    def _merge_system_text(self, *parts: Optional[str]) -> Optional[str]:
        cleaned = [str(part).strip() for part in parts if str(part or "").strip()]
        return "\n\n".join(cleaned) if cleaned else None

    def _build_messages_for_llm(self, *, system_text: Optional[str], history_for_llm: List[Dict[str, Any]], user_text: str) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for item in history_for_llm:
            if isinstance(item, dict):
                messages.append(dict(item))
        messages.append({"role": "user", "content": user_text})
        return messages

    @staticmethod
    def _parse_json_object_text(text: Any) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if 0 <= start < end:
            snippet = raw[start : end + 1]
            try:
                obj2 = json.loads(snippet)
                return obj2 if isinstance(obj2, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _clip_text(value: Any, max_len: int = 240) -> str:
        clean = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if len(clean) <= max_len:
            return clean
        return clean[: max(0, max_len - 3)].rstrip() + "..."

    @staticmethod
    def _task_text_list(value: Any, max_items: int = 24, max_len: int = 280) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            clean = str(item or "").strip()
            if not clean:
                continue
            if len(clean) > max_len:
                clean = clean[: max_len - 3].rstrip() + "..."
            out.append(clean)
            if len(out) >= max_items:
                break
        return out

    def _default_task_state(self) -> Dict[str, Any]:
        return {
            "task": "",
            "state": "active",
            "step": 0,
            "total": 0,
            "current": "",
            "expected_action": "answer_or_clarify",
            "is_paused": False,
            "plan": [],
            "done": [],
            "goal": "",
            "clarifications": [],
            "constraints": [],
            "terms": [],
            "open_questions": [],
            "done_steps": [],
            "schema_version": 1,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_reset_decision": "",
        }

    def _normalize_task_state(self, task_state: Any, *, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = self._default_task_state()
        if isinstance(fallback, dict):
            base.update(fallback)
        raw = task_state if isinstance(task_state, dict) else {}

        goal = str(raw["goal"] if ("goal" in raw and raw.get("goal") is not None) else base.get("goal") or "").strip()
        clarifications = self._task_text_list(
            raw["clarifications"] if "clarifications" in raw else base.get("clarifications"),
            max_items=32,
            max_len=320,
        )
        constraints = self._task_text_list(
            raw["constraints"] if "constraints" in raw else base.get("constraints"),
            max_items=32,
            max_len=320,
        )
        terms = self._task_text_list(
            raw["terms"] if "terms" in raw else base.get("terms"),
            max_items=32,
            max_len=220,
        )
        open_questions = self._task_text_list(
            raw["open_questions"] if "open_questions" in raw else base.get("open_questions"),
            max_items=32,
            max_len=320,
        )
        done_steps = self._task_text_list(
            raw["done_steps"] if "done_steps" in raw else base.get("done_steps"),
            max_items=64,
            max_len=320,
        )

        is_paused = bool(raw.get("is_paused", base.get("is_paused", False)))
        state = str(raw.get("state") or base.get("state") or "active").strip().lower()
        if state not in {"planning", "active", "paused", "done"}:
            state = "paused" if is_paused else "active"
        if is_paused:
            state = "paused"
        elif state == "paused":
            state = "active"

        task_text = str(raw["task"] if "task" in raw else base.get("task") or "").strip()
        if not task_text:
            task_text = goal

        step = len(done_steps)
        total = max(step + len(open_questions), step)
        current = str(raw["current"] if "current" in raw else base.get("current") or "").strip()
        if not current:
            current = open_questions[0] if open_questions else ""
        expected_action = str(raw["expected_action"] if "expected_action" in raw else base.get("expected_action") or "").strip() or "answer_or_clarify"

        normalized = {
            "task": task_text,
            "state": state,
            "step": step,
            "total": total,
            "current": current,
            "expected_action": expected_action,
            "is_paused": is_paused,
            "plan": list(open_questions),
            "done": list(done_steps),
            "goal": goal,
            "clarifications": clarifications,
            "constraints": constraints,
            "terms": terms,
            "open_questions": open_questions,
            "done_steps": done_steps,
            "schema_version": 1,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_reset_decision": str(raw.get("last_reset_decision") or base.get("last_reset_decision") or "").strip(),
        }
        return normalized

    def _get_branch_task_state(self, branch: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_task_state(branch.get("task_state"), fallback=None)
        branch["task_state"] = normalized
        return normalized

    def _set_branch_task_state(self, branch: Dict[str, Any], task_state: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_task_state(task_state, fallback=self._get_branch_task_state(branch))
        branch["task_state"] = normalized
        return normalized

    @staticmethod
    def _looks_like_task_forget_request(user_text: str) -> bool:
        clean = str(user_text or "").strip().lower()
        if not clean:
            return False
        markers = (
            "забудь задачу",
            "сбрось задачу",
            "очисти задачу",
            "удали задачу",
            "forget task",
            "clear task",
            "reset task",
            "delete task memory",
            "forget context",
            "clear context",
        )
        return any(marker in clean for marker in markers)

    async def _llm_should_forget_task_state(
        self,
        *,
        user_text: str,
        task_state: Dict[str, Any],
        model: str,
        req_id: str,
    ) -> Dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify if the user explicitly asked to clear current task memory.\n"
                    "Return strict JSON only: "
                    "{\"should_forget\":true|false,\"reason\":\"...\",\"confidence\":0..1}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_text": user_text,
                        "current_task_state": {
                            "goal": task_state.get("goal"),
                            "open_questions": task_state.get("open_questions"),
                            "done_steps": task_state.get("done_steps"),
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        parts: List[str] = []
        try:
            async for chunk in self.gpt.stream_chat(
                messages=messages,
                model=model,
                max_tokens=180,
                temperature=0.0,
                trace_id=f"{req_id}:task_reset_classifier",
            ):
                if chunk:
                    parts.append(str(chunk))
            payload = self._parse_json_object_text("".join(parts))
            return {
                "should_forget": bool(payload.get("should_forget", False)),
                "reason": str(payload.get("reason") or "").strip(),
                "confidence": float(payload.get("confidence") or 0.0),
            }
        except Exception as e:
            return {"should_forget": False, "reason": f"classifier_failed: {e}", "confidence": 0.0}

    async def _llm_update_task_state(
        self,
        *,
        user_text: str,
        assistant_text: str,
        current_task_state: Dict[str, Any],
        model: str,
        req_id: str,
    ) -> Dict[str, Any]:
        schema_hint = {
            "goal": "string",
            "clarifications": ["string"],
            "constraints": ["string"],
            "terms": ["string"],
            "open_questions": ["string"],
            "done_steps": ["string"],
        }
        prompt_system = (
            "You maintain task memory for a coding assistant conversation.\n"
            "Return strict JSON only with keys: goal, clarifications, constraints, terms, open_questions, done_steps.\n"
            "Rules:\n"
            "1) Preserve existing facts unless clearly contradicted.\n"
            "2) Keep items concise and deduplicated.\n"
            "3) Do not invent facts absent from the dialogue.\n"
            "4) Task is ongoing by default; unresolved items stay in open_questions.\n"
            "5) Output valid JSON object only."
        )
        payload_user = {
            "schema": schema_hint,
            "current_task_state": {
                "goal": current_task_state.get("goal") or "",
                "clarifications": current_task_state.get("clarifications") or [],
                "constraints": current_task_state.get("constraints") or [],
                "terms": current_task_state.get("terms") or [],
                "open_questions": current_task_state.get("open_questions") or [],
                "done_steps": current_task_state.get("done_steps") or [],
            },
            "new_turn": {
                "user": user_text,
                "assistant": assistant_text,
            },
        }
        parts: List[str] = []
        try:
            async for chunk in self.gpt.stream_chat(
                messages=[
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": json.dumps(payload_user, ensure_ascii=False)},
                ],
                model=model,
                max_tokens=700,
                temperature=0.0,
                trace_id=f"{req_id}:task_state_update",
            ):
                if chunk:
                    parts.append(str(chunk))
            parsed = self._parse_json_object_text("".join(parts))
            next_state = self._normalize_task_state(parsed, fallback=current_task_state)
            return next_state
        except Exception as e:
            fallback = self._normalize_task_state(current_task_state, fallback=current_task_state)
            fallback["last_reset_decision"] = str(fallback.get("last_reset_decision") or "")
            self._log("WARN", "TASK_STATE_UPDATE_FAILED", {"req_id": req_id, "error": str(e)})
            return fallback

    def _recent_turns_for_rag(self, history: List[Dict[str, Any]], max_pairs: int = 3) -> List[Dict[str, str]]:
        if not isinstance(history, list) or not history:
            return []
        pairs: List[Dict[str, str]] = []
        pending_user = ""
        for msg in history:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            text = self._clip_text(msg.get("content") or "", 420)
            if not text:
                continue
            if role == "user":
                pending_user = text
                continue
            if role == "assistant":
                pairs.append({"user": pending_user, "assistant": text})
                pending_user = ""
        if pending_user:
            pairs.append({"user": pending_user, "assistant": ""})
        return pairs[-max_pairs:]

    async def _llm_expand_rag_query(
        self,
        *,
        user_text: str,
        task_state: Dict[str, Any],
        history: List[Dict[str, Any]],
        model: str,
        req_id: str,
    ) -> Dict[str, Any]:
        current_goal = str(task_state.get("goal") or "").strip()
        open_questions = self._task_text_list(task_state.get("open_questions"), max_items=8, max_len=220)
        done_steps = self._task_text_list(task_state.get("done_steps"), max_items=8, max_len=220)
        terms = self._task_text_list(task_state.get("terms"), max_items=12, max_len=120)
        recent_turns = self._recent_turns_for_rag(history, max_pairs=3)
        prompt_system = (
            "You optimize repository retrieval queries.\n"
            "Given the last user message plus active task context, resolve pronouns/references "
            "(like 'этот класс', 'данный метод') into explicit entities.\n"
            "Return strict JSON only: {\"rag_query\":\"...\",\"reason\":\"...\"}.\n"
            "Do not add unrelated details. Keep it concise and specific for semantic code search."
        )
        payload = {
            "last_user_message": user_text,
            "active_task_goal": current_goal,
            "task_terms": terms,
            "open_questions": open_questions,
            "done_steps": done_steps,
            "recent_turns": recent_turns,
        }
        parts: List[str] = []
        try:
            async for chunk in self.gpt.stream_chat(
                messages=[
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model=model,
                max_tokens=220,
                temperature=0.0,
                trace_id=f"{req_id}:rag_query_expand",
            ):
                if chunk:
                    parts.append(str(chunk))
            parsed = self._parse_json_object_text("".join(parts))
            candidate = str(parsed.get("rag_query") or "").strip()
            if candidate:
                return {
                    "query": candidate,
                    "reason": str(parsed.get("reason") or "").strip(),
                    "used_llm": True,
                    "error": "",
                }
            return {
                "query": user_text,
                "reason": "empty_rag_query_from_llm",
                "used_llm": True,
                "error": "",
            }
        except Exception as e:
            return {
                "query": user_text,
                "reason": "rag_query_expand_failed",
                "used_llm": False,
                "error": str(e),
            }

    @staticmethod
    def _source_match_key(item: Dict[str, Any]) -> Tuple[str, str, int, int, str]:
        return (
            str(item.get("file") or "").strip().lower(),
            str(item.get("section") or "").strip().lower(),
            int(item.get("start_line") or 0),
            int(item.get("end_line") or 0),
            str(item.get("strategy") or "").strip().lower(),
        )

    async def _synthesize_rag_answer(
        self,
        *,
        user_text: str,
        draft_answer: str,
        rag_chunks: List[Dict[str, Any]],
        rag_similarity_threshold: float,
    ) -> Tuple[str, Dict[str, Any]]:
        if not rag_chunks:
            unknown_text = "Ответ: Не знаю. В контексте RAG не найдено релевантных фрагментов. Уточните запрос."
            return unknown_text, {
                "is_unknown": True,
                "unknown_reason": "RAG returned no chunks",
                "sources_count": 0,
                "synthesis_error": "",
                "forced_unknown_by_threshold": True,
                "max_score": 0.0,
            }

        max_score = max(float(item.get("score") or 0.0) for item in rag_chunks)
        forced_unknown_by_threshold = bool(max_score < float(rag_similarity_threshold))

        compact_chunks: List[Dict[str, Any]] = []
        by_chunk_id: Dict[str, Dict[str, Any]] = {}
        by_match_key: Dict[Tuple[str, str, int, int, str], List[Dict[str, Any]]] = {}
        for item in rag_chunks:
            chunk_id = str(item.get("chunk_id") or "").strip()
            normalized = {
                "chunk_id": chunk_id,
                "source": str(item.get("source") or "repo"),
                "file": str(item.get("file") or ""),
                "title": str(item.get("title") or item.get("file") or ""),
                "section": str(item.get("section") or ""),
                "strategy": str(item.get("strategy") or ""),
                "start_line": int(item.get("start_line") or 0),
                "end_line": int(item.get("end_line") or 0),
                "score": float(item.get("score") or 0.0),
                "content": str(item.get("content") or ""),
            }
            compact_chunks.append(
                {
                    **normalized,
                    "content": str(normalized["content"])[:1800],
                }
            )
            if chunk_id:
                by_chunk_id[chunk_id] = normalized
            key = self._source_match_key(normalized)
            bucket = by_match_key.get(key)
            if bucket is None:
                by_match_key[key] = [normalized]
            else:
                bucket.append(normalized)

        prompt_system = (
            "You are a strict RAG answer formatter.\n"
            "Return strict JSON only with this schema:\n"
            "{\n"
            "  \"answer_text\": \"string\",\n"
            "  \"sources\": [\n"
            "    {\n"
            "      \"chunk_id\": \"string\",\n"
            "      \"source\": \"string\",\n"
            "      \"file\": \"string\",\n"
            "      \"title\": \"string\",\n"
            "      \"section\": \"string\",\n"
            "      \"strategy\": \"string\",\n"
            "      \"start_line\": 0,\n"
            "      \"end_line\": 0,\n"
            "      \"quote\": \"string\"\n"
            "    }\n"
            "  ],\n"
            "  \"unknown\": {\n"
            "    \"is_unknown\": false,\n"
            "    \"reason\": \"string\"\n"
            "  }\n"
            "}\n"
            "Rules:\n"
            "1) Use only provided chunks as facts. No external knowledge.\n"
            "2) One key thesis in answer_text must be backed by at least one source with quote.\n"
            "3) If information is insufficient, set unknown.is_unknown=true and explicitly say you do not know in answer_text.\n"
            "4) Never invent chunk ids, files, sections, lines or quotes.\n"
            "5) Keep quotes short exact snippets from chunk content.\n"
        )
        prompt_user_payload = {
            "question": user_text,
            "draft_answer": draft_answer,
            "force_unknown_due_threshold": forced_unknown_by_threshold,
            "threshold": float(rag_similarity_threshold),
            "max_score": float(max_score),
            "chunks": compact_chunks,
        }

        synthesis_error = ""
        parsed: Dict[str, Any] = {}
        try:
            raw = await asyncio.to_thread(
                self.rag._chat_completion_text,
                messages=[
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": json.dumps(prompt_user_payload, ensure_ascii=False)},
                ],
                max_tokens=1800,
            )
            parsed = self._parse_json_object_text(raw)
        except Exception as e:
            synthesis_error = str(e)

        answer_text = str(parsed.get("answer_text") or "").strip() if isinstance(parsed, dict) else ""
        unknown_data = parsed.get("unknown") if isinstance(parsed.get("unknown"), dict) else {}
        is_unknown = bool(unknown_data.get("is_unknown", False))
        unknown_reason = str(unknown_data.get("reason") or "").strip()
        raw_sources = parsed.get("sources") if isinstance(parsed.get("sources"), list) else []

        normalized_sources: List[Dict[str, Any]] = []
        seen_keys = set()
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("chunk_id") or "").strip()
            base: Optional[Dict[str, Any]] = None
            if cid and cid in by_chunk_id:
                base = by_chunk_id[cid]
            else:
                key = self._source_match_key(
                    {
                        "file": item.get("file"),
                        "section": item.get("section"),
                        "start_line": item.get("start_line"),
                        "end_line": item.get("end_line"),
                        "strategy": item.get("strategy"),
                    }
                )
                matched = by_match_key.get(key) or []
                if len(matched) == 1:
                    base = matched[0]
            if base is None:
                continue
            base_content = str(base.get("content") or "")
            quote = str(item.get("quote") or "").strip()
            if not quote:
                quote = self._clip_text(base_content, 220)
            else:
                normalized_content = " ".join(base_content.split()).lower()
                normalized_quote = " ".join(quote.split()).lower()
                if normalized_quote and normalized_quote not in normalized_content:
                    self._log(
                        "WARN",
                        "RAG_QUOTE_REPLACED",
                        {
                            "reason": "llm_quote_not_found_in_chunk",
                            "chunk_id": str(base.get("chunk_id") or ""),
                            "file": str(base.get("file") or ""),
                            "section": str(base.get("section") or ""),
                            "llm_quote": quote,
                        },
                    )
                    quote = self._clip_text(base_content, 220)
            record = {
                "chunk_id": str(base.get("chunk_id") or ""),
                "source": str(base.get("source") or "repo"),
                "file": str(base.get("file") or ""),
                "title": str(base.get("title") or base.get("file") or ""),
                "section": str(base.get("section") or ""),
                "strategy": str(base.get("strategy") or ""),
                "start_line": int(base.get("start_line") or 0),
                "end_line": int(base.get("end_line") or 0),
                "quote": quote,
            }
            dedup_key = (record["chunk_id"], record["quote"])
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            normalized_sources.append(record)

        if forced_unknown_by_threshold:
            is_unknown = True
            if not unknown_reason:
                unknown_reason = (
                    f"Недостаточная релевантность контекста: max_score={max_score:.4f} "
                    f"ниже порога {float(rag_similarity_threshold):.4f}."
                )

        if not answer_text and draft_answer.strip():
            answer_text = draft_answer.strip()

        if (not is_unknown) and not normalized_sources:
            is_unknown = True
            if not unknown_reason:
                unknown_reason = "LLM не смогла корректно привязать ответ к источникам RAG."

        answer_lower = answer_text.lower()
        if is_unknown and "не знаю" not in answer_lower:
            reason_tail = f" {unknown_reason}" if unknown_reason else ""
            answer_text = f"Не знаю.{reason_tail}".strip()

        if not answer_text:
            answer_text = "Не знаю. Недостаточно данных для обоснованного ответа."
            is_unknown = True
            if not unknown_reason:
                unknown_reason = "empty_answer_after_synthesis"

        lines: List[str] = [f"Ответ: {answer_text}"]
        if normalized_sources:
            lines.append("")
            lines.append("Источники:")
            for idx, src in enumerate(normalized_sources, start=1):
                quote_clean = self._clip_text(src.get("quote") or "", 260)
                lines.append(
                    f"{idx}) {src['source']} | chunk_id={src['chunk_id']} | {src['file']} | {src['title']} | "
                    f"{src['section']} | {src['strategy']} | "
                    f"lines {src['start_line']}-{src['end_line']} (цитата: \"{quote_clean}\")"
                )
        elif is_unknown:
            lines.append("")
            lines.append("Источники: нет подтвержденных релевантных источников для ответа.")

        return "\n".join(lines).strip(), {
            "is_unknown": bool(is_unknown),
            "unknown_reason": unknown_reason,
            "sources_count": len(normalized_sources),
            "synthesis_error": synthesis_error,
            "forced_unknown_by_threshold": bool(forced_unknown_by_threshold),
            "max_score": float(max_score),
        }

    async def _validate_invariants(self, *, req_id: str, messages: List[Dict[str, Any]], invariants_text: Optional[str], model: str) -> Dict[str, Any]:
        if not invariants_text:
            return {"decision": "pass", "reason": "no invariants"}
        validator_messages = [
            {
                "role": "system",
                "content": (
                    "You validate whether the planned assistant response context respects invariants.\n"
                    "Return strict JSON only: {\"decision\":\"pass|warn\",\"reason\":\"...\"}.\n"
                    "Do not call tools."
                ),
            },
            {"role": "user", "content": f"{invariants_text}\n\nConversation snapshot:\n{json.dumps(messages, ensure_ascii=False)}"},
        ]
        try:
            parts: List[str] = []
            async for chunk in self.gpt.stream_chat(
                messages=validator_messages,
                model=model,
                max_tokens=200,
                temperature=0.0,
                trace_id=f"{req_id}:invariants",
            ):
                if chunk:
                    parts.append(str(chunk))
            text = "".join(parts).strip()
            payload = json.loads(text) if text.startswith("{") else {}
            decision = str(payload.get("decision") or "pass").strip().lower()
            if decision not in {"pass", "warn"}:
                decision = "warn"
            return {"decision": decision, "reason": str(payload.get("reason") or text)}
        except Exception as e:
            return {"decision": "warn", "reason": f"invariants validator failed: {e}"}

    def _serialize_mcp_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        return [{"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in tools]

    def _is_scheduler_task_intent(self, user_text: str) -> bool:
        clean = str(user_text or "").strip().lower()
        if not clean:
            return False
        scheduler_markers = (
            "кажд", "каждую", "каждый", "ежеднев", "еженед", "раз в", "по распис", "расписан",
            "запланир", "планировщ", "создай зада", "создай напомин", "напомин", "автоматически",
            "регулярно", "через кажды", "разово в", "выполняй",
        )
        action_markers = ("отправ", "присыл", "шли", "уведом", "погод")
        return any(marker in clean for marker in scheduler_markers) and any(marker in clean for marker in action_markers)

    def _build_scheduler_task_protocol(self, user_text: str, tools: List[Any]) -> Optional[str]:
        if not self._is_scheduler_task_intent(user_text):
            return None
        if not any(str(getattr(tool, "name", "") or "") == "scheduler__create_task" for tool in tools):
            return None
        examples = scheduler_contract_examples()
        return "\n".join([
            "[ACTIVE_TASK_PROTOCOL]",
            "Current user request is a scheduler/task-creation request.",
            "Your primary goal is to create a scheduler task, not to satisfy the request with one-off tool calls.",
            "Allowed next actions:",
            "1. Ask a clarifying question if schedule or required delivery data is missing.",
            "2. Call scheduler__get_scheduler_hints if you are unsure about payload shape.",
            "3. Call scheduler__create_task with the exact canonical schema once payload is fully known.",
            "Forbidden while handling this request:",
            "- Do not call gismeteo__get_current_weather or telegram__send_message as a substitute for creating the scheduler task.",
            "- Do not use functions.* prefixes in scheduler steps.",
            "- Do not put message templates into top-level template_text.",
            "- Put templated chat_id/text into steps[*].arguments_template only.",
            f"Canonical interval example: {json.dumps(examples['interval'], ensure_ascii=False)}",
        ])

    def _build_scheduler_repair_instruction(self, error_payload: Dict[str, Any]) -> str:
        examples = scheduler_contract_examples()
        return "\n".join([
            "[SCHEDULER_REPAIR_PROTOCOL]",
            "Your previous scheduler__create_task call was rejected.",
            f"Validation error: {str(error_payload.get('message') or '').strip()}",
            "You must repair the scheduler payload and retry scheduler__create_task, or call scheduler__get_scheduler_hints if any field shape is still unclear.",
            "Do not switch to one-off business tool calls.",
            f"Canonical interval example: {json.dumps(examples['interval'], ensure_ascii=False)}",
        ])

    def _build_tool_usage_instruction(self, tools: List[Any]) -> Optional[str]:
        if not tools:
            return None
        lines = [
            "[TOOLS_POLICY]\n"
            "- Use a tool only when it materially helps answer the user.\n"
            "- Never invent required arguments or extra keys.\n"
            "- If a required argument is missing, ask a clarifying question instead of calling the tool.\n"
            "- Use tool names exactly as published by MCP.\n"
            "- Tool arguments must be a strict JSON object that matches the tool schema.\n"
            "- If a tool returns an error, explain the error briefly and ask only for the missing concrete data."
        ]
        if any(str(getattr(tool, "name", "") or "") == "scheduler__create_task" for tool in tools):
            examples = scheduler_contract_examples()
            lines.extend([
                "[SCHEDULER_CREATE_TASK_CONTRACT]",
                "- If you are not fully sure, call scheduler__get_scheduler_hints before scheduler__create_task.",
                "- scheduler__create_task accepts only title, schedule_type, schedule, steps, optional template_text, metadata.",
                "- Each steps item accepts only tool, arguments, arguments_template, save_result_as.",
                "- Never send recipient_name, parameters, functions.* prefixes or strings like {result.steps.0.summary}.",
                f"- Canonical interval example: {json.dumps(examples['interval'], ensure_ascii=False)}",
            ])
        return "\n".join(lines)

    def _augment_validation_error(self, tool_name: str, error_payload: Dict[str, Any]) -> Dict[str, Any]:
        if str(tool_name or "").strip() != "scheduler__create_task":
            return error_payload
        examples = scheduler_contract_examples()
        payload = dict(error_payload)
        payload["message"] = (
            str(error_payload.get("message") or "Invalid scheduler payload.")
            + " scheduler__create_task must use schedule_type + schedule + steps. "
            + "steps must be a plain list of {tool, arguments|arguments_template, save_result_as}. "
            + f"Canonical example: {json.dumps(examples['interval'], ensure_ascii=False)}"
        )
        return payload

    def _validate_scheduler_step_routes(self, scheduler_args: Dict[str, Any], tools: List[Any]) -> Optional[Dict[str, Any]]:
        tool_map = {str(getattr(tool, "name", "") or ""): tool for tool in tools}
        errors: List[str] = []
        steps = scheduler_args.get("steps") if isinstance(scheduler_args.get("steps"), list) else []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            step_tool_name = str(step.get("tool") or "").strip()
            step_tool = tool_map.get(step_tool_name)
            if step_tool is None:
                errors.append(f"steps[{index}].tool unknown tool '{step_tool_name}'")
                continue
            schema = step_tool.input_schema if isinstance(step_tool.input_schema, dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = [str(item) for item in (schema.get("required") if isinstance(schema.get("required"), list) else []) if str(item) != "trace_id"]
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            arguments_template = step.get("arguments_template") if isinstance(step.get("arguments_template"), dict) else {}
            provided_keys = set(arguments.keys()) | set(arguments_template.keys())
            missing = [key for key in required if key not in provided_keys]
            if missing:
                errors.append(f"steps[{index}] missing required nested tool arguments: {', '.join(missing)}")
            if schema.get("additionalProperties") is False:
                unknown_keys = [key for key in provided_keys if key not in properties]
                if unknown_keys:
                    errors.append(f"steps[{index}] unexpected nested tool arguments: {', '.join(sorted(unknown_keys))}")
            if arguments:
                schema_errors = validate_json_value({**arguments, "trace_id": "nested-trace"}, schema, path=f"$.steps[{index}].arguments")
                for error in schema_errors:
                    if "missing required field" in error and any(key in error for key in required):
                        continue
                    if "unexpected field 'trace_id'" in error:
                        continue
                    errors.append(error.replace("nested-trace", "<trace_id>"))
        if errors:
            return {
                "is_error": True,
                "error_type": "invalid_scheduler_step_route",
                "message": " | ".join(errors),
                "tool_name": "scheduler__create_task",
                "arguments": scheduler_args,
            }
        return None

    def _validate_tool_arguments(self, tool_name: str, tool_args: Dict[str, Any], tools: List[Any]) -> Optional[Dict[str, Any]]:
        name = str(tool_name or "").strip()
        if not isinstance(tool_args, dict):
            return {
                "is_error": True,
                "error_type": "invalid_tool_arguments_type",
                "message": f"Tool '{name}' arguments must be a JSON object.",
                "tool_name": name,
                "arguments": tool_args,
            }
        match = next((tool for tool in tools if str(getattr(tool, "name", "") or "") == name), None)
        if match is None:
            return {
                "is_error": True,
                "error_type": "unknown_tool",
                "message": f"Tool '{name}' is not available in current MCP tools list.",
                "tool_name": name,
                "arguments": tool_args,
            }

        if name == "scheduler__create_task":
            scheduler_errors = validate_scheduler_create_task_payload(tool_args)
            if scheduler_errors:
                return self._augment_validation_error(name, {
                    "is_error": True,
                    "error_type": "invalid_scheduler_task_payload",
                    "message": " | ".join(scheduler_errors),
                    "tool_name": name,
                    "arguments": tool_args,
                })
            nested_route_error = self._validate_scheduler_step_routes(tool_args, tools)
            if nested_route_error is not None:
                return self._augment_validation_error(name, nested_route_error)

        schema = match.input_schema if isinstance(match.input_schema, dict) else {}
        schema_errors = validate_json_value(tool_args, schema, path="$") if schema else []
        if schema_errors:
            return self._augment_validation_error(name, {
                "is_error": True,
                "error_type": "schema_validation_error",
                "message": " | ".join(schema_errors),
                "tool_name": name,
                "arguments": tool_args,
                "required": schema.get("required") if isinstance(schema.get("required"), list) else [],
            })

        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        placeholders = {
            "owner", "repo", "username", "organization", "org", "branch", "tag", "query", "path", "sha",
            "owner_name", "repo_name", "your_repo", "your_owner", "example", "value",
        }
        bad_fields: List[str] = []
        for key, value in tool_args.items():
            if not isinstance(value, str):
                continue
            clean = value.strip().lower()
            if not clean:
                continue
            if clean in placeholders or clean == key.strip().lower():
                bad_fields.append(key)
                continue
            if clean.startswith("your_") or clean.endswith("_name"):
                bad_fields.append(key)
        if bad_fields:
            descriptions = []
            for field in bad_fields:
                desc = ""
                if isinstance(properties.get(field), dict):
                    desc = str(properties[field].get("description") or "").strip()
                descriptions.append(f"{field}{f' ({desc})' if desc else ''}")
            return {
                "is_error": True,
                "error_type": "placeholder_arguments",
                "message": f"Tool arguments look like placeholders or invented values: {', '.join(descriptions)}.",
                "tool_name": name,
                "arguments": tool_args,
                "required": schema.get("required") if isinstance(schema.get("required"), list) else [],
            }
        return None

    async def _get_mcp_tools(self) -> Tuple[List[Any], Dict[str, Any]]:
        try:
            tools = await self.mcp.list_tools()
            return tools, {"enabled": True, "connected": True, "tools_count": len(tools)}
        except Exception as e:
            return [], {"enabled": self.mcp.enabled, "connected": False, "error": str(e), "tools_count": 0}

    def _extract_tool_calls(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        result: List[Dict[str, Any]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            result.append(
                {
                    "id": str(item.get("id") or str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or "").strip(),
                        "arguments": str(function.get("arguments") or "{}"),
                    },
                }
            )
        return result

    async def _handle_ping(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "pong"})

    async def _handle_list_sessions(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        sessions = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in self.memory_store.list_sessions()
        ]
        await self._send_json(writer, {"type": "sessions", "sessions": sessions})

    async def _handle_get_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        await self._send_json_maybe_chunked(writer, {"type": "session", "session": session})

    async def _handle_reset_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        ok = self.memory_store.delete_session_file(session_id)
        await self._send_json(writer, {"type": "ok" if ok else "error", "message": "" if ok else "session not found"})

    async def _handle_list_branches(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        items = []
        for bid, branch in branches.items():
            if not isinstance(branch, dict):
                continue
            items.append(
                {
                    "branch_id": bid,
                    "title": str(branch.get("title") or bid),
                    "created_at": str(branch.get("created_at") or ""),
                    "updated_at": str(branch.get("updated_at") or ""),
                }
            )
        await self._send_json(writer, {"type": "branches", "branches": items, "active_branch": session.get("active_branch") or "main"})

    async def _handle_switch_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        branch_id = str(request.get("branch_id") or "").strip()
        if not session_id or not branch_id:
            await self._send_error(writer, "session_id and branch_id are required")
            return
        session = self.memory_store.load_session(session_id)
        bid, _ = self._ensure_branch(session, branch_id)
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "ok", "active_branch": bid})

    async def _handle_list_checkpoints(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        branch_id = str(request.get("branch_id") or "").strip() or None
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        _, branch = self._ensure_branch(session, branch_id)
        checkpoints = branch.get("checkpoints") if isinstance(branch.get("checkpoints"), list) else []
        await self._send_json(writer, {"type": "checkpoints", "checkpoints": checkpoints, "active_branch": session.get("active_branch")})

    async def _handle_create_checkpoint(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        _, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        checkpoint = self._make_checkpoint(branch.get("history") if isinstance(branch.get("history"), list) else [], str(request.get("name") or ""))
        branch.setdefault("checkpoints", []).append(checkpoint)
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "checkpoint_created", "checkpoint": checkpoint})

    async def _handle_create_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        from_branch_id = str(request.get("from_branch_id") or "").strip()
        checkpoint_id = str(request.get("checkpoint_id") or "").strip()
        if not session_id or not from_branch_id or not checkpoint_id:
            await self._send_error(writer, "session_id, from_branch_id and checkpoint_id are required")
            return
        session = self.memory_store.load_session(session_id)
        try:
            new_bid = self._create_branch_from_checkpoint(session, from_branch_id, checkpoint_id, str(request.get("new_branch_name") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "branch_created", "branch_id": new_bid})

    async def _handle_get_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        await self._send_json(
            writer,
            {
                "type": "memory",
                "active_branch": active_branch,
                "memory_layers": branch.get("memory_layers") or {},
                "facts": branch.get("facts") or {},
            },
        )

    async def _handle_save_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        layer = str(request.get("layer") or "").strip()
        value = str(request.get("value") or "").strip()
        key = str(request.get("key") or "").strip()
        if not session_id or not layer or not value:
            await self._send_error(writer, "session_id, layer and value are required")
            return
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        memory_layers = self._copy_memory_layers(branch.get("memory_layers"))
        self._save_memory_item(memory_layers, layer, key, value)
        branch["memory_layers"] = memory_layers
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "ok", "active_branch": active_branch, "memory_layers": memory_layers})

    async def _handle_get_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        profile = self.profile_store.get_profile(profile_name)
        if not isinstance(profile, dict):
            await self._send_error(writer, "profile not found")
            return
        await self._send_json(writer, {"type": "profile", "profile": profile})

    async def _handle_save_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            saved = self.profile_store.save_profile(str(request.get("profile_name") or ""), str(request.get("description") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_delete_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        saved = self.profile_store.delete_profile(str(request.get("profile_name") or ""))
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_set_active_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        saved = self.profile_store.set_active_profile(str(request.get("profile_name") or ""))
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_get_profile_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "profile_state",
                "profiles": state.get("available_profiles") or [],
                "active_profile": state.get("active_profile") or "",
            },
        )

    async def _handle_get_invariants_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_invariants_state()
        await self._send_json(
            writer,
            {
                "type": "invariants_state",
                "invariants": state.get("invariants") or {},
                "invariant_policy": state.get("invariant_policy") or {},
            },
        )

    async def _handle_save_invariant(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            state = self.profile_store.save_invariant_value(str(request.get("key") or ""), str(request.get("value") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(writer, {"type": "ok", **state})

    async def _handle_set_invariant_policy(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            state = self.profile_store.set_invariant_policy(str(request.get("key") or ""), str(request.get("policy") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(writer, {"type": "ok", **state})

    async def _handle_get_task_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        task_state = self._get_branch_task_state(branch)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": task_state})

    async def _handle_generate_task_plan(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        task_text = str(request.get("task") or "").strip()
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        current = self._get_branch_task_state(branch)
        merged = dict(current)
        if task_text and not str(merged.get("goal") or "").strip():
            merged["goal"] = task_text
            merged["task"] = task_text
        merged["state"] = "active"
        merged["is_paused"] = False
        merged = self._set_branch_task_state(branch, merged)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": merged})

    async def _handle_confirm_task_plan(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._get_branch_task_state(branch)
        state["state"] = "active"
        state["is_paused"] = False
        state = self._set_branch_task_state(branch, state)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_pause_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._get_branch_task_state(branch)
        state["is_paused"] = True
        state["state"] = "paused"
        state = self._set_branch_task_state(branch, state)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_resume_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._get_branch_task_state(branch)
        state["is_paused"] = False
        state["state"] = "active"
        state = self._set_branch_task_state(branch, state)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_next_task_step(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._set_branch_task_state(branch, self._get_branch_task_state(branch))
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_update_task_progress(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._get_branch_task_state(branch)
        done_item = str(request.get("done_item") or "").strip()
        if done_item:
            done_steps = state.get("done_steps") if isinstance(state.get("done_steps"), list) else []
            if done_item not in done_steps:
                done_steps.append(done_item)
            state["done_steps"] = done_steps
        current = str(request.get("current") or "").strip()
        if current:
            state["current"] = current
        expected_action = str(request.get("expected_action") or "").strip()
        if expected_action:
            state["expected_action"] = expected_action
        state = self._set_branch_task_state(branch, state)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_delete_task(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        branch_id = str(request.get("branch_id") or "").strip() or None
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, branch_id)
        state = self._default_task_state()
        state["last_reset_decision"] = "manual_delete_via_rpc"
        state = self._set_branch_task_state(branch, state)
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "task_state", "active_branch": active_branch, "task_state": state})

    async def _handle_mcp_status(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "mcp_status", "status": await self.mcp.status()})

    async def _handle_mcp_list_tools(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            tools = await self.mcp.list_tools()
            await self._send_json(writer, {"type": "mcp_tools", "connected": True, "tools": self._serialize_mcp_tools(tools)})
        except Exception as e:
            await self._send_json(writer, {"type": "mcp_tools", "connected": False, "tools": [], "error": str(e)})

    async def _handle_mcp_call_tool(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        tool_name = str(request.get("tool_name") or request.get("name") or "").strip()
        arguments = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
        try:
            result = await self.mcp.call_tool(tool_name, arguments)
            await self._send_json(writer, {"type": "mcp_tool_result", "ok": True, "tool_name": tool_name, "result": result})
        except Exception as e:
            await self._send_json(writer, {"type": "mcp_tool_result", "ok": False, "tool_name": tool_name, "error": str(e)})

    async def _handle_stream_chat(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        req_id = str(uuid.uuid4())
        session_id = str(request.get("session_id") or "").strip() or str(uuid.uuid4())
        model = str(request.get("model") or self.gpt.model).strip()
        max_tokens = int(request.get("max_tokens") or 800)
        temperature = request.get("temperature")
        keep_last_n = int(request.get("keep_last_n") or 10)
        strategy = str(request.get("context_strategy") or "sliding").strip().lower()
        user_text = str(request.get("user_text") or "").strip()
        use_profile = bool(request.get("use_profile", False))
        use_rag = bool(request.get("use_rag", False))
        use_mcp_tools = bool(request.get("use_mcp_tools", True))
        rag_strategy = str(request.get("rag_strategy") or "fixed").strip().lower() or "fixed"
        try:
            rag_top_k = max(1, int(request.get("rag_top_k") or self.rag_top_k_default))
        except Exception:
            rag_top_k = self.rag_top_k_default
        rag_rewrite_enabled = bool(request.get("rag_rewrite_enabled", self.rag_rewrite_default))
        rag_rerank_mode = str(request.get("rag_rerank_mode") or self.rag_rerank_mode_default).strip().lower() or "none"
        try:
            rag_top_k_before = max(1, int(request.get("rag_top_k_before") or self.rag_top_k_before_default))
        except Exception:
            rag_top_k_before = self.rag_top_k_before_default
        try:
            rag_similarity_threshold = float(request.get("rag_similarity_threshold"))
        except Exception:
            rag_similarity_threshold = self.rag_similarity_threshold_default
        try:
            rag_top_k_after = max(1, int(request.get("rag_top_k_after") or self.rag_top_k_after_default))
        except Exception:
            rag_top_k_after = self.rag_top_k_after_default
        rag_similarity_threshold = max(0.0, min(1.0, rag_similarity_threshold))
        if not user_text:
            await self._send_error(writer, "user_text is required")
            return

        session = self.memory_store.load_session(session_id)
        self.memory_store.set_title_if_empty(session, user_text)
        branch_key = str(request.get("branch_id") or "").strip() or None
        active_branch, branch = self._ensure_branch(session, branch_key if strategy == "branching" else "main")
        history = branch.get("history") if isinstance(branch.get("history"), list) else []
        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        memory_layers = self._copy_memory_layers(branch.get("memory_layers"))
        task_state = self._get_branch_task_state(branch)
        reset_decision: Dict[str, Any] = {"checked": False, "should_forget": False, "reason": "", "confidence": 0.0}

        if self._looks_like_task_forget_request(user_text):
            reset_decision["checked"] = True
            await self._send_task_signal(writer, "task_memory", "Проверяю намерение очистить память задачи через LLM")
            llm_reset = await self._llm_should_forget_task_state(
                user_text=user_text,
                task_state=task_state,
                model=model,
                req_id=req_id,
            )
            reset_decision.update(llm_reset)
            if bool(llm_reset.get("should_forget")):
                task_state = self._default_task_state()
                task_state["last_reset_decision"] = f"accepted: {str(llm_reset.get('reason') or '').strip()}"
                branch["task_state"] = task_state
                await self._send_task_signal(writer, "task_memory", "Память задачи очищена")
            else:
                task_state["last_reset_decision"] = f"rejected: {str(llm_reset.get('reason') or '').strip()}"
                branch["task_state"] = task_state
                await self._send_task_signal(writer, "task_memory", "Очистка task memory отклонена")

        explicit_memory = request.get("memory_write") if isinstance(request.get("memory_write"), dict) else None
        if isinstance(explicit_memory, dict):
            self._save_memory_item(
                memory_layers,
                str(explicit_memory.get("layer") or ""),
                str(explicit_memory.get("key") or ""),
                str(explicit_memory.get("value") or ""),
            )
            branch["memory_layers"] = memory_layers

        user_text_for_api = user_text
        system_text = None
        rag_context_text = None
        rag_chunks: List[Dict[str, Any]] = []
        rag_query_for_retrieval = user_text
        rag_query_expand_meta: Dict[str, Any] = {"used_llm": False, "reason": "", "error": ""}
        if strategy == "facts":
            facts, cleaned_user_text = parse_facts_and_strip_user_text(user_text=user_text, prev_facts=facts)
            branch["facts"] = facts
            user_text_for_api = cleaned_user_text or "Учти обновленные факты и продолжай."
            facts_system_text, history_for_llm = build_facts_strategy(history, facts, keep_last_n)
            system_text = facts_system_text
        elif strategy == "summary":
            system_text, history_for_llm, summary_text = build_summary_strategy(history, keep_last_n, previous_summary=str(branch.get("summary") or ""))
            branch["summary"] = summary_text
        else:
            history_for_llm = build_sliding_window(history, keep_last_n)

        if use_rag:
            rag_query_expand_meta = await self._llm_expand_rag_query(
                user_text=user_text,
                task_state=task_state,
                history=history,
                model=model,
                req_id=req_id,
            )
            rag_query_for_retrieval = str(rag_query_expand_meta.get("query") or user_text).strip() or user_text
            self._log(
                "INFO",
                "RAG_RETRIEVE_REQUEST",
                {
                    "req_id": req_id,
                    "session_id": session_id,
                    "branch_id": active_branch,
                    "user_text": user_text,
                    "rag_query_retrieval": rag_query_for_retrieval,
                    "rag_query_expand_used_llm": bool(rag_query_expand_meta.get("used_llm")),
                    "rag_query_expand_reason": str(rag_query_expand_meta.get("reason") or ""),
                    "rag_query_expand_error": str(rag_query_expand_meta.get("error") or ""),
                    "strategy": rag_strategy,
                    "top_k": rag_top_k,
                    "rewrite_enabled": rag_rewrite_enabled,
                    "rerank_mode": rag_rerank_mode,
                    "top_k_before": rag_top_k_before,
                    "similarity_threshold": rag_similarity_threshold,
                    "top_k_after": rag_top_k_after,
                },
            )
            await self._send_task_signal(
                writer,
                "rag",
                "Выполняю retrieval по RAG-индексу",
                {
                    "strategy": rag_strategy,
                    "top_k": rag_top_k,
                    "rewrite": rag_rewrite_enabled,
                    "rerank_mode": rag_rerank_mode,
                    "top_k_before": rag_top_k_before,
                    "similarity_threshold": rag_similarity_threshold,
                    "top_k_after": rag_top_k_after,
                    "query_for_retrieval": self._clip_text(rag_query_for_retrieval, 180),
                },
            )
            try:
                rag_chunks = await asyncio.to_thread(
                    self.rag.retrieve,
                    query=rag_query_for_retrieval,
                    strategy=rag_strategy,
                    top_k=rag_top_k,
                    rewrite_enabled=rag_rewrite_enabled,
                    rerank_mode=rag_rerank_mode,
                    top_k_before=rag_top_k_before,
                    similarity_threshold=rag_similarity_threshold,
                    top_k_after=rag_top_k_after,
                )
                rag_context_text = self.rag.build_rag_context(rag_chunks)
                self._log(
                    "INFO",
                    "RAG_RETRIEVE_RESPONSE",
                    {
                        "req_id": req_id,
                        "session_id": session_id,
                        "branch_id": active_branch,
                        "rag_query_retrieval": rag_query_for_retrieval,
                        "chunks_count": len(rag_chunks),
                        "chunks": [
                            {
                                "rank": int(item.get("rank") or idx + 1),
                                "chunk_id": str(item.get("chunk_id") or ""),
                                "file": str(item.get("file") or ""),
                                "section": str(item.get("section") or ""),
                                "start_line": int(item.get("start_line") or 0),
                                "end_line": int(item.get("end_line") or 0),
                                "score": float(item.get("score") or 0.0),
                            }
                            for idx, item in enumerate(rag_chunks)
                            if isinstance(item, dict)
                        ],
                        "rag_last_run_meta": dict(self.rag.last_run_meta) if isinstance(getattr(self.rag, "last_run_meta", None), dict) else {},
                    },
                )
            except RagError as e:
                self._log(
                    "ERROR",
                    "RAG_RETRIEVE_ERROR",
                    {
                        "req_id": req_id,
                        "session_id": session_id,
                        "branch_id": active_branch,
                        "rag_query_retrieval": rag_query_for_retrieval,
                        "message": str(e),
                    },
                )
                await self._send_error(writer, f"RAG error: {e}")
                return

        memory_system_text = self._build_memory_system_text(memory_layers)
        profile_state = self.profile_store.get_state()
        active_profile = str(profile_state.get("active_profile") or "").strip()
        profile_description = ""
        if active_profile:
            profile = self.profile_store.get_profile(active_profile)
            if isinstance(profile, dict):
                profile_description = str(profile.get("description") or "")
        profile_system_text = self._build_profile_system_text(active_profile, profile_description) if use_profile else None
        invariants_state = build_invariants_state(self.profile_store.get_invariants_state())
        invariants_text = build_invariants_system_text(
            invariants_state.get("invariants") or {},
            invariants_state.get("invariant_policy") or {},
        )
        if use_mcp_tools:
            await self._send_task_signal(writer, "tools", "Получаю список инструментов MCP")
            mcp_tools, mcp_info = await self._get_mcp_tools()
            tools_policy_text = self._build_tool_usage_instruction(mcp_tools)
            scheduler_task_protocol = self._build_scheduler_task_protocol(user_text, mcp_tools)
            scheduler_task_mode = bool(scheduler_task_protocol)
            mcp_info = {**mcp_info, "provided_to_llm": True, "disabled_by_user": False}
        else:
            mcp_tools = []
            mcp_info = {
                "enabled": bool(self.mcp.enabled),
                "connected": bool(self.mcp.enabled),
                "tools_count": 0,
                "provided_to_llm": False,
                "disabled_by_user": True,
            }
            tools_policy_text = None
            scheduler_task_protocol = None
            scheduler_task_mode = False
            await self._send_task_signal(writer, "tools", "Подача MCP-инструментов в LLM отключена")
        system_text = self._merge_system_text(
            memory_system_text,
            profile_system_text,
            system_text,
            rag_context_text,
            invariants_text,
            tools_policy_text,
            scheduler_task_protocol,
        )
        working_messages = self._build_messages_for_llm(system_text=system_text, history_for_llm=history_for_llm, user_text=user_text_for_api)
        self._log(
            "INFO",
            "CHAT_CONTEXT",
            {
                "req_id": req_id,
                "session_id": session_id,
                "branch_id": active_branch,
                "strategy": strategy,
                "system_text": system_text or "",
                "messages": working_messages,
            },
        )

        invariants_check = await self._validate_invariants(
            req_id=req_id,
            messages=working_messages,
            invariants_text=invariants_text,
            model=model,
        )
        self._log("INFO", "INVARIANTS_VALIDATION", invariants_check)

        openai_tools = [tool.to_openai_tool() for tool in mcp_tools]
        await self._send_task_signal(writer, "tools", "Инструменты MCP готовы", {"tools_count": len(openai_tools), "use_mcp_tools": use_mcp_tools})
        self._log("INFO", "MCP_TOOLS_FOR_LLM", {"req_id": req_id, "mcp_info": mcp_info, "tools": openai_tools, "use_mcp_tools": use_mcp_tools})

        usage_agg: Dict[str, Any] = {}
        final_answer = ""
        tool_events: List[Dict[str, Any]] = []
        rag_output_meta: Dict[str, Any] = {
            "is_unknown": False,
            "unknown_reason": "",
            "sources_count": 0,
            "synthesis_error": "",
            "forced_unknown_by_threshold": False,
            "max_score": 0.0,
        }
        loop_guard = 0
        max_tool_iterations = max(8, int(str(os.getenv("AGENT_MAX_TOOL_ITERATIONS", "32")).strip() or "32"))
        rag_meta = dict(self.rag.last_run_meta) if use_rag and isinstance(getattr(self.rag, "last_run_meta", None), dict) else {}
        relay_stream_chunks = not use_rag
        history.append({"role": "user", "content": user_text})

        while loop_guard < max_tool_iterations:
            loop_guard += 1
            await self._send_task_signal(writer, "llm", "Запрашиваю следующий шаг у модели", {"iteration": loop_guard})
            stream_started = False

            async for chunk in self.gpt.stream_chat(
                messages=working_messages,
                model=model,
                max_tokens=max_tokens,
                temperature=float(temperature) if temperature is not None else None,
                trace_id=f"{req_id}:loop:{loop_guard}",
                tools=openai_tools if openai_tools else None,
                tool_choice="auto",
            ):
                if not chunk:
                    continue
                if not stream_started:
                    stream_started = True
                    await self._send_task_signal(writer, "final", "Начинаю потоковую генерацию ответа")
                if relay_stream_chunks:
                    await self._send_json(writer, {"type": "chunk", "chunk": chunk})

            usage_agg = self._merge_usage(usage_agg, self.gpt.last_usage if isinstance(self.gpt.last_usage, dict) else {})
            assistant_message = self.gpt.last_message if isinstance(self.gpt.last_message, dict) else {}
            tool_calls = self._extract_tool_calls(assistant_message)
            self._log("INFO", "LLM_ASSISTANT_MESSAGE", assistant_message)

            if tool_calls:
                assistant_entry = {"role": "assistant", "content": assistant_message.get("content"), "tool_calls": tool_calls}
                working_messages.append(assistant_entry)
                history.append(assistant_entry)
                await self._send_task_signal(writer, "llm", "Модель запросила инструменты", {"iteration": loop_guard, "tool_calls": len(tool_calls)})
                for tool_call in tool_calls:
                    tool_name = str((tool_call.get("function") or {}).get("name") or "").strip()
                    raw_args = str((tool_call.get("function") or {}).get("arguments") or "{}")
                    validation_error = None
                    try:
                        parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                    except Exception as e:
                        tool_args = {}
                        validation_error = {
                            "is_error": True,
                            "error_type": "invalid_tool_arguments_json",
                            "message": f"Tool '{tool_name}' arguments must be valid JSON object: {e}",
                            "tool_name": tool_name,
                            "raw_arguments": raw_args,
                        }
                    else:
                        if not isinstance(parsed_args, dict):
                            tool_args = {}
                            validation_error = {
                                "is_error": True,
                                "error_type": "invalid_tool_arguments_type",
                                "message": f"Tool '{tool_name}' arguments must decode to a JSON object, got {type(parsed_args).__name__}.",
                                "tool_name": tool_name,
                                "raw_arguments": raw_args,
                            }
                        else:
                            tool_args = parsed_args
                            validation_error = self._validate_tool_arguments(tool_name, tool_args, mcp_tools)
                    await self._send_task_signal(writer, "tool_call", f"Вызов инструмента {tool_name}", {"tool_name": tool_name, "arguments": tool_args, "iteration": loop_guard})
                    try:
                        if validation_error is not None:
                            raise MCPClientError(validation_error.get("message") or "Invalid tool arguments")
                        tool_result = await self.mcp.call_tool(tool_name, tool_args)
                    except MCPClientError as e:
                        if validation_error is not None:
                            tool_result = dict(validation_error)
                        else:
                            tool_result = {"is_error": True, "error": str(e), "tool_name": tool_name, "arguments": tool_args}
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": self._serialize_tool_result_for_history(tool_result),
                    }
                    working_messages.append(tool_message)
                    history.append(tool_message)
                    if scheduler_task_mode and tool_name == "scheduler__create_task" and bool(tool_result.get("is_error")):
                        repair_message = {
                            "role": "system",
                            "content": self._build_scheduler_repair_instruction(tool_result),
                        }
                        working_messages.append(repair_message)
                    await self._send_task_signal(writer, "tool_result", f"Инструмент {tool_name} завершён", {"tool_name": tool_name, "result": tool_result, "iteration": loop_guard})
                    event = {"tool_call": tool_call, "tool_result": tool_result}
                    tool_events.append(event)
                    self._log("INFO", "LLM_TOOL_ROUNDTRIP", event)
                continue

            final_answer = str(assistant_message.get("content") or "")
            break

        if not final_answer:
            final_answer = "LLM не вернула финальный текстовый ответ."
        if use_rag:
            await self._send_task_signal(writer, "rag", "Формирую структурированный ответ с источниками и цитатами")
            final_answer, rag_output_meta = await self._synthesize_rag_answer(
                user_text=user_text,
                draft_answer=final_answer,
                rag_chunks=rag_chunks,
                rag_similarity_threshold=rag_similarity_threshold,
            )
            await self._send_json(writer, {"type": "chunk", "chunk": final_answer + "\n"})

        history.append({"role": "assistant", "content": final_answer})
        await self._send_task_signal(writer, "task_memory", "Обновляю память задачи через LLM")
        task_state = await self._llm_update_task_state(
            user_text=user_text,
            assistant_text=final_answer,
            current_task_state=task_state,
            model=model,
            req_id=req_id,
        )
        if reset_decision.get("checked"):
            decision_text = "accepted" if reset_decision.get("should_forget") else "rejected"
            reason = str(reset_decision.get("reason") or "").strip()
            task_state["last_reset_decision"] = f"{decision_text}: {reason}".strip(": ")
        branch["task_state"] = self._normalize_task_state(task_state, fallback=task_state)

        self._sync_short_term_from_history(memory_layers, history)
        branch["history"] = history
        branch["memory_layers"] = memory_layers
        branch["facts"] = facts
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)

        token_stats = {
            "user_text_tokens_est": self._estimate_tokens_text(user_text_for_api),
            "context_tokens_est": self._estimate_tokens_messages(working_messages[:-1]) if working_messages else 0,
            "assistant_tokens": int(usage_agg.get("completion_tokens") or usage_agg.get("output_tokens") or self._estimate_tokens_text(final_answer)),
            "total_tokens_call": int(usage_agg.get("total_tokens") or 0),
            "dialog_tokens_est": self._estimate_tokens_messages(history),
            "model_context_limit": self._resolve_context_limit(model),
            "may_exceed_context": False,
        }
        token_stats["may_exceed_context"] = bool(
            token_stats["context_tokens_est"] + token_stats["user_text_tokens_est"] > token_stats["model_context_limit"]
        )
        message_stats = {
            "strategy": strategy,
            "branch_id": active_branch,
            "use_profile": use_profile,
            "active_profile": active_profile,
            "profile_description_len": len(profile_description),
            "profile_applied": bool(profile_system_text),
            "keep_last_n": keep_last_n,
            "sent_messages": len(working_messages),
            "facts_count": len(facts) if isinstance(facts, dict) else 0,
            "memory_layers_counts": {
                "short_term": len(memory_layers.get("short_term") or []),
                "working": len(memory_layers.get("working") or {}),
                "long_term": len(memory_layers.get("long_term") or {}),
            },
            "invariants_decision": invariants_check.get("decision") or "pass",
            "invariants_reason": invariants_check.get("reason") or "",
            "task_state": str(task_state.get("state") or ""),
            "task_step": int(task_state.get("step") or 0),
            "task_total": int(task_state.get("total") or 0),
            "task_paused": bool(task_state.get("is_paused", False)),
            "task_injected": bool(task_state.get("goal") or task_state.get("open_questions") or task_state.get("done_steps")),
            "task_reset_checked": bool(reset_decision.get("checked")),
            "task_reset_approved": bool(reset_decision.get("should_forget")),
            "task_reset_reason": str(reset_decision.get("reason") or ""),
            "tool_iterations": loop_guard,
            "tools_available": len(mcp_tools),
            "use_mcp_tools": use_mcp_tools,
            "use_rag": use_rag,
            "rag_strategy": rag_strategy if use_rag else "off",
            "rag_top_k": rag_top_k if use_rag else 0,
            "rag_sources_count": int(rag_output_meta.get("sources_count") or len(rag_chunks)) if use_rag else 0,
            "rag_rewrite_enabled": bool(rag_meta.get("rewrite_enabled", rag_rewrite_enabled)) if use_rag else False,
            "rag_rewrite_applied": bool(rag_meta.get("rewrite_applied", False)) if use_rag else False,
            "rag_rerank_mode": str(rag_meta.get("rerank_mode") or rag_rerank_mode) if use_rag else "off",
            "rag_top_k_before": int(rag_meta.get("top_k_before") or rag_top_k_before) if use_rag else 0,
            "rag_similarity_threshold": float(rag_meta.get("similarity_threshold") or rag_similarity_threshold) if use_rag else 0.0,
            "rag_top_k_after": int(rag_meta.get("top_k_after") or rag_top_k_after) if use_rag else 0,
            "rag_candidates_before": int(rag_meta.get("initial_candidates_count") or rag_top_k_before) if use_rag else 0,
            "rag_candidates_after": int(rag_meta.get("final_candidates_count") or len(rag_chunks)) if use_rag else 0,
            "rag_rewrite_error": str(rag_meta.get("rewrite_error") or "") if use_rag else "",
            "rag_rerank_error": str(rag_meta.get("rerank_error") or "") if use_rag else "",
            "rag_query_original": str(user_text) if use_rag else "",
            "rag_query_retrieval": str(rag_query_for_retrieval) if use_rag else "",
            "rag_query_effective": str(rag_meta.get("effective_query") or rag_query_for_retrieval) if use_rag else "",
            "rag_query_expand_used_llm": bool(rag_query_expand_meta.get("used_llm")) if use_rag else False,
            "rag_query_expand_reason": str(rag_query_expand_meta.get("reason") or "") if use_rag else "",
            "rag_query_expand_error": str(rag_query_expand_meta.get("error") or "") if use_rag else "",
            "rag_unknown": bool(rag_output_meta.get("is_unknown")) if use_rag else False,
            "rag_unknown_reason": str(rag_output_meta.get("unknown_reason") or "") if use_rag else "",
            "rag_forced_unknown_by_threshold": bool(rag_output_meta.get("forced_unknown_by_threshold")) if use_rag else False,
            "rag_max_score": float(rag_output_meta.get("max_score") or 0.0) if use_rag else 0.0,
            "rag_synthesis_error": str(rag_output_meta.get("synthesis_error") or "") if use_rag else "",
        }
        cost_rub = self._calc_cost_rub(model_id=model, usage=usage_agg)
        await self._send_task_signal(writer, "done", "Ответ сформирован", {"tool_iterations": loop_guard})
        done_payload = {
            "type": "done",
            "model": model,
            "endpoint": "chat",
            "usage": usage_agg,
            "cost_rub": cost_rub,
            "session_id": session_id,
            "title": session.get("title") or "",
            "active_branch": active_branch,
            "message_stats": message_stats,
            "facts": facts if isinstance(facts, dict) else {},
            "memory_layers": memory_layers,
            "token_stats": token_stats,
            "profile_info": {
                "use_profile": use_profile,
                "active_profile": active_profile,
                "profile_description_len": len(profile_description),
                "profile_applied": bool(profile_system_text),
            },
            "task_state": task_state,
            "mcp_info": {**mcp_info, "tools": self._serialize_mcp_tools(mcp_tools)},
            "tool_events": tool_events,
        }
        self._log("INFO", "CHAT_DONE", done_payload)
        await self._send_json(writer, done_payload)

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
                    self._log("ERROR", "RPC_REQUEST_INVALID_JSON", {"peer": str(peer), "raw_line": line.decode("utf-8", errors="replace")[:800]})
                    await self._send_json(writer, {"type": "error", "message": "Invalid JSON"})
                    continue
                action = str(request.get("action") or "").strip()
                self._log("INFO", "RPC_REQUEST", {"peer": str(peer), "action": action, "request": request})
                handler = self._action_handlers.get(action)
                if handler is None:
                    self._log("WARN", "RPC_REQUEST_UNKNOWN_ACTION", {"peer": str(peer), "action": action, "request": request})
                    await self._send_error(writer, "Unknown action")
                    continue
                await handler(request, writer)
                self._log("INFO", "RPC_REQUEST_DONE", {"peer": str(peer), "action": action})
        except Exception as e:
            self.logger.write("ERROR", "handle_client", extra=str(e))
            self.logger.write("ERROR", "TRACEBACK", extra=traceback.format_exc())
            try:
                await self._send_json(writer, {"type": "error", "message": str(e) or "Unknown error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.logger.write("INFO", "Клиент отключился", extra=str(peer))

    async def run(self) -> None:
        await self.preload_pricing()
        await self._ensure_scheduler_worker()
        status = await self.mcp.status()
        self._log("INFO", "MCP_STATUS_AT_STARTUP", status)
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.write("INFO", "Агент запущен и слушает", extra=addrs)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self._stop_scheduler_worker()


async def main() -> None:
    agent = LLMAgentServer()
    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


