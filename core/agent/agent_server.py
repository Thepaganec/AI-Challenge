import sys
sys.dont_write_bytecode = True

import asyncio
import json
import os
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
from core.agent.strategies import (
    build_sliding_window,
    parse_facts_from_user_text,
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
        self.logger = AgentFileLogger(logs_dir=self.base_dir, prefix="agentlogs")
        self.logger.cleanup_old_logs(keep_days=3)

        self.memory_dir = os.path.join(self.base_dir, "memory")
        self.memory_store = AgentMemoryStore(base_dir=self.memory_dir)
        self.profile_store = AgentProfileStore(file_path=os.path.join(self.base_dir, "profiles.json"))

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
            "stream_chat": self._handle_stream_chat,
        }
        self._model_context_limit: Dict[str, int] = {
            "gpt-3.5-turbo": 16384,
            "gpt-4o-mini": 128000,
            "gpt-4o": 128000,
            "gpt-5.2-chat-latest": 400000,
        }

    async def preload_pricing(self) -> None:
        try:
            self.logger.write("INFO", "Загрузка тарифов ProxyAPI (pricing/list)...")
            self.pricing_cache = await self.gpt.get_pricing_rub_per_1m()
            self.logger.write("SUCCESS", "Тарифы загружены", extra=f"models={len(self.pricing_cache)}")
        except Exception as e:
            self.logger.write("WARN", "Не удалось загрузить тарифы ProxyAPI", extra=str(e))
            self.pricing_cache = {}

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

    async def _send_json(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)
        await writer.drain()

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

    def _ensure_branch_memory_model(self, branch: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(branch.get("summary"), str):
            branch["summary"] = ""
        branch["memory_layers"] = self._copy_memory_layers(branch.get("memory_layers"))
        return branch["memory_layers"]

    def _estimate_tokens_text(self, text: str) -> int:
        clean = (text or "").strip()
        if not clean:
            return 0
        return max(1, int(len(clean) / 4))

    def _estimate_tokens_messages(self, messages: List[Dict[str, str]]) -> int:
        total = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            total += self._estimate_tokens_text(str(msg.get("content") or ""))
            total += 4
        return int(total)

    def _resolve_context_limit(self, model: str) -> int:
        model_id = (model or "").strip()
        return int(self._model_context_limit.get(model_id, 128000))

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

    def _merge_system_text(self, *chunks: Optional[str]) -> Optional[str]:
        parts = [str(c).strip() for c in chunks if isinstance(c, str) and str(c).strip()]
        if not parts:
            return None
        return "\n\n".join(parts)

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

    def _ensure_title(self, session: Dict[str, Any], user_text: str) -> None:
        self.memory_store.set_title_if_empty(session, user_text)

    async def _send_error(self, writer: asyncio.StreamWriter, message: str) -> None:
        await self._send_json(writer, {"type": "error", "message": message})

    async def _handle_ping(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "pong"})

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

    async def _handle_reset_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = (request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        self.memory_store.delete_session_file(session_id)
        await self._send_json(writer, {"type": "ok"})

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

        strategy = (request.get("context_strategy") or "sliding").strip().lower()
        use_profile = bool(request.get("use_profile", False))

        strategy_for_context = strategy if strategy in ("sliding", "facts", "summary", "branching") else "sliding"
        strategy_display = strategy_for_context

        session = self.memory_store.load_session(session_id)
        self._ensure_title(session, user_text)

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

        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        facts = parse_facts_from_user_text(user_text, facts)
        branch["facts"] = facts

        system_text = None
        history_for_llm: List[Dict[str, str]] = []

        if strategy_for_context == "facts":
            system_text, history_for_llm = build_facts_strategy(history, facts, keep_last_n)
        elif strategy_for_context == "summary":
            previous_summary = str(branch.get("summary") or "")
            system_text, history_for_llm, updated_summary = build_summary_strategy(
                history=history,
                keep_last_n=keep_last_n,
                previous_summary=previous_summary,
            )
            branch["summary"] = updated_summary
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
                user_text=user_text,
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
            gen = self.gpt.stream_chat(
                user_text=user_text,
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

            sent_messages = int(len(history_for_llm) + (1 if system_text else 0) + 1)
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total_tokens_call = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

            full_history_tokens_est = self._estimate_tokens_messages(history)
            context_tokens_est = self._estimate_tokens_messages(history_for_llm) + self._estimate_tokens_text(system_text or "")
            user_tokens_est = self._estimate_tokens_text(user_text)
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

            message_stats = {
                "strategy": strategy_display,
                "branch_id": bid,
                "use_profile": use_profile,
                "active_profile": active_profile or "Без профиля",
                "profile_description_len": int(len(profile_description)),
                "profile_applied": profile_applied,
                "keep_last_n": int(keep_last_n),
                "sent_messages": int(sent_messages),
                "facts_count": int(len(facts) if isinstance(facts, dict) else 0),
                "memory_layers_counts": {
                    "short_term": int(len(memory_layers.get("short_term") or [])) if isinstance(memory_layers.get("short_term"), list) else 0,
                    "working": int(len(memory_layers.get("working") or {})) if isinstance(memory_layers.get("working"), dict) else 0,
                    "long_term": int(len(memory_layers.get("long_term") or {})) if isinstance(memory_layers.get("long_term"), dict) else 0,
                },
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
                    "facts": facts if isinstance(facts, dict) else {},
                    "memory_layers": memory_layers,
                    "token_stats": token_stats,
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
