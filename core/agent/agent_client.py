import asyncio
import json
import sys
from typing import Any, AsyncIterator, Dict, List, Optional

sys.dont_write_bytecode = True


class AgentClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout_sec: int = 10):
        self.host = host
        self.port = port
        self.timeout_sec = timeout_sec
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._conn_lock = asyncio.Lock()

        self.last_usage: Dict[str, Any] = {}
        self.last_cost_rub: Optional[float] = None
        self.last_model: Optional[str] = None
        self.last_endpoint: Optional[str] = None
        self.last_title: Optional[str] = None
        self.last_message_stats: Dict[str, Any] = {}
        self.last_active_branch: Optional[str] = None
        self.last_facts: Optional[dict] = None
        self.last_memory_layers: Optional[dict] = None
        self.last_token_stats: Dict[str, Any] = {}
        self.last_profile_info: Dict[str, Any] = {}
        self.last_invariants_state: Dict[str, Any] = {}
        self.last_task_state: Dict[str, Any] = {}
        self.last_task_signal: Dict[str, Any] = {}
        self.last_mcp_info: Dict[str, Any] = {}
        self.last_tool_events: List[Dict[str, Any]] = []
        self.on_task_signal = None

    def _is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and self._reader is not None

    async def _ensure_connection(self) -> None:
        if self._is_connected():
            return
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port, limit=20_000_000)

    async def _close_connection(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    def _is_connection_error(self, e: Exception) -> bool:
        return isinstance(e, (ConnectionError, BrokenPipeError, ConnectionResetError, OSError, asyncio.IncompleteReadError))

    async def _rpc(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with self._conn_lock:
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    await self._ensure_connection()
                    assert self._reader is not None and self._writer is not None
                    self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                    await self._writer.drain()

                    while True:
                        line = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout_sec)
                        if not line:
                            raise ConnectionError("Connection closed by server")
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                        if msg.get("type") == "task_signal":
                            continue
                        break

                    if msg.get("type") == "chunked_start":
                        orig = msg.get("orig_type")
                        chunks = int(msg.get("chunks") or 0)
                        parts = [""] * chunks
                        while True:
                            line2 = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout_sec)
                            if not line2:
                                raise ConnectionError("Connection closed during chunked response")
                            m2 = json.loads(line2.decode("utf-8", errors="replace"))
                            if m2.get("type") == "chunked_part" and m2.get("orig_type") == orig:
                                idx = int(m2.get("i") or 0)
                                if 0 <= idx < chunks:
                                    parts[idx] = str(m2.get("data") or "")
                                continue
                            if m2.get("type") == "chunked_end" and m2.get("orig_type") == orig:
                                break
                            if m2.get("type") == "error":
                                return m2
                        return json.loads("".join(parts))
                    return msg
                except Exception as e:
                    last_error = e
                    if self._is_connection_error(e) and attempt == 0:
                        await self._close_connection()
                        continue
                    raise
            if last_error:
                raise last_error
            raise RuntimeError("RPC failed")

    def _raise_if_error(self, msg: Dict[str, Any]) -> None:
        if msg.get("type") == "error":
            raise RuntimeError(msg.get("message") or "Agent error")

    async def ping(self) -> bool:
        try:
            msg = await self._rpc({"action": "ping"})
            return msg.get("type") == "pong"
        except Exception:
            return False

    async def list_sessions(self) -> List[dict]:
        msg = await self._rpc({"action": "list_sessions"})
        return msg.get("sessions") or [] if msg.get("type") == "sessions" else []

    async def get_session(self, session_id: str) -> Optional[dict]:
        msg = await self._rpc({"action": "get_session", "session_id": session_id})
        if msg.get("type") == "session":
            return msg.get("session")
        self._raise_if_error(msg)
        return None

    async def reset_session(self, session_id: str) -> bool:
        msg = await self._rpc({"action": "reset_session", "session_id": session_id})
        self._raise_if_error(msg)
        return msg.get("type") == "ok"

    async def switch_branch(self, session_id: str, branch_id: str) -> str:
        msg = await self._rpc({"action": "switch_branch", "session_id": session_id, "branch_id": branch_id})
        self._raise_if_error(msg)
        return str(msg.get("active_branch") or branch_id or "main").strip() or "main"

    async def create_checkpoint(self, session_id: str, branch_id: str, name: str = "") -> str:
        msg = await self._rpc({"action": "create_checkpoint", "session_id": session_id, "branch_id": branch_id, "name": name})
        if msg.get("type") == "checkpoint_created":
            checkpoint = msg.get("checkpoint") if isinstance(msg.get("checkpoint"), dict) else {}
            return str(checkpoint.get("id") or "")
        self._raise_if_error(msg)
        return ""

    async def create_branch(self, session_id: str, from_branch_id: str, checkpoint_id: str, new_branch_name: str = "") -> str:
        msg = await self._rpc(
            {
                "action": "create_branch",
                "session_id": session_id,
                "from_branch_id": from_branch_id,
                "checkpoint_id": checkpoint_id,
                "new_branch_name": new_branch_name,
            }
        )
        if msg.get("type") == "branch_created":
            return str(msg.get("branch_id") or "")
        self._raise_if_error(msg)
        return ""

    async def get_profile(self, profile_name: str) -> Optional[Dict[str, Any]]:
        msg = await self._rpc({"action": "get_profile", "profile_name": profile_name})
        self._raise_if_error(msg)
        return msg.get("profile") if msg.get("type") == "profile" else None

    async def save_profile(self, profile_name: str, description: str) -> Dict[str, Any]:
        msg = await self._rpc({"action": "save_profile", "profile_name": profile_name, "description": description})
        self._raise_if_error(msg)
        return {"ok": msg.get("type") == "ok", "profiles": msg.get("profiles") or [], "active_profile": msg.get("active_profile") or ""}

    async def delete_profile(self, profile_name: str) -> Dict[str, Any]:
        msg = await self._rpc({"action": "delete_profile", "profile_name": profile_name})
        self._raise_if_error(msg)
        return {"ok": msg.get("type") == "ok", "profiles": msg.get("profiles") or [], "active_profile": msg.get("active_profile") or ""}

    async def set_active_profile(self, profile_name: str) -> Dict[str, Any]:
        msg = await self._rpc({"action": "set_active_profile", "profile_name": profile_name})
        self._raise_if_error(msg)
        return {"ok": msg.get("type") == "ok", "profiles": msg.get("profiles") or [], "active_profile": msg.get("active_profile") or ""}

    async def get_profile_state(self) -> Dict[str, Any]:
        msg = await self._rpc({"action": "get_profile_state"})
        self._raise_if_error(msg)
        return {"profiles": msg.get("profiles") or [], "active_profile": msg.get("active_profile") or ""} if msg.get("type") == "profile_state" else {"profiles": [], "active_profile": ""}

    async def get_invariants_state(self) -> Dict[str, Any]:
        msg = await self._rpc({"action": "get_invariants_state"})
        self._raise_if_error(msg)
        if msg.get("type") == "invariants_state":
            state = {
                "invariants": msg.get("invariants") if isinstance(msg.get("invariants"), dict) else {},
                "invariant_policy": msg.get("invariant_policy") if isinstance(msg.get("invariant_policy"), dict) else {},
            }
            self.last_invariants_state = state
            return state
        return {"invariants": {}, "invariant_policy": {}}

    async def save_invariant(self, key: str, value: str) -> Dict[str, Any]:
        msg = await self._rpc({"action": "save_invariant", "key": key, "value": value})
        self._raise_if_error(msg)
        state = {
            "invariants": msg.get("invariants") if isinstance(msg.get("invariants"), dict) else {},
            "invariant_policy": msg.get("invariant_policy") if isinstance(msg.get("invariant_policy"), dict) else {},
        }
        self.last_invariants_state = state
        return state

    async def set_invariant_policy(self, key: str, policy: str) -> Dict[str, Any]:
        msg = await self._rpc({"action": "set_invariant_policy", "key": key, "policy": policy})
        self._raise_if_error(msg)
        state = {
            "invariants": msg.get("invariants") if isinstance(msg.get("invariants"), dict) else {},
            "invariant_policy": msg.get("invariant_policy") if isinstance(msg.get("invariant_policy"), dict) else {},
        }
        self.last_invariants_state = state
        return state

    async def get_memory(self, session_id: str, branch_id: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"action": "get_memory", "session_id": session_id}
        if branch_id:
            payload["branch_id"] = branch_id
        msg = await self._rpc(payload)
        self._raise_if_error(msg)
        if msg.get("type") == "memory":
            layers = msg.get("memory_layers") if isinstance(msg.get("memory_layers"), dict) else {}
            self.last_memory_layers = layers
            return {"active_branch": msg.get("active_branch"), "memory_layers": layers, "facts": msg.get("facts") or {}}
        return {"active_branch": branch_id or "main", "memory_layers": {}, "facts": {}}

    async def save_memory(self, session_id: str, branch_id: str, layer: str, value: str, key: str = "") -> Dict[str, Any]:
        msg = await self._rpc(
            {
                "action": "save_memory",
                "session_id": session_id,
                "branch_id": branch_id,
                "layer": layer,
                "key": key,
                "value": value,
            }
        )
        self._raise_if_error(msg)
        layers = msg.get("memory_layers") if isinstance(msg.get("memory_layers"), dict) else {}
        self.last_memory_layers = layers
        return {"ok": msg.get("type") == "ok", "active_branch": msg.get("active_branch"), "memory_layers": layers}

    async def mcp_status(self) -> Dict[str, Any]:
        msg = await self._rpc({"action": "mcp_status"})
        self._raise_if_error(msg)
        return msg.get("status") if isinstance(msg.get("status"), dict) else {}

    async def mcp_list_tools(self) -> Dict[str, Any]:
        msg = await self._rpc({"action": "mcp_list_tools"})
        self._raise_if_error(msg)
        return {"connected": bool(msg.get("connected")), "tools": msg.get("tools") or [], "error": msg.get("error") or ""}

    async def mcp_call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        msg = await self._rpc({"action": "mcp_call_tool", "tool_name": tool_name, "arguments": arguments or {}})
        self._raise_if_error(msg)
        return {
            "ok": bool(msg.get("ok")),
            "tool_name": msg.get("tool_name") or tool_name,
            "result": msg.get("result"),
            "error": msg.get("error") or "",
        }

    async def stream_chat(
        self,
        user_text: str,
        model: str,
        endpoint: str,
        max_tokens: int,
        temperature: Optional[float],
        session_id: str,
        branch_id: Optional[str],
        keep_last_n: int,
        context_strategy: str,
        memory_write: Optional[Dict[str, str]] = None,
        use_profile: bool = False,
        summary_config: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        self.last_usage = {}
        self.last_cost_rub = None
        self.last_model = None
        self.last_endpoint = None
        self.last_title = None
        self.last_message_stats = {}
        self.last_active_branch = None
        self.last_facts = None
        self.last_memory_layers = None
        self.last_token_stats = {}
        self.last_profile_info = {}
        self.last_invariants_state = {}
        self.last_task_state = {}
        self.last_task_signal = {}
        self.last_mcp_info = {}
        self.last_tool_events = []

        request: Dict[str, Any] = {
            "action": "stream_chat",
            "session_id": session_id,
            "user_text": user_text,
            "model": model,
            "endpoint": endpoint,
            "max_tokens": int(max_tokens),
            "keep_last_n": int(keep_last_n),
            "context_strategy": str(context_strategy or "sliding"),
            "use_profile": bool(use_profile),
        }
        if temperature is not None:
            request["temperature"] = temperature
        if request["context_strategy"] == "branching" and branch_id:
            request["branch_id"] = str(branch_id)
        if isinstance(memory_write, dict):
            request["memory_write"] = memory_write
        if request["context_strategy"] == "summary" and isinstance(summary_config, dict):
            request["summary_config"] = summary_config

        async with self._conn_lock:
            try:
                await self._ensure_connection()
                assert self._reader is not None and self._writer is not None
                self._writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                await self._writer.drain()

                while True:
                    line = await self._reader.readline()
                    if not line:
                        raise ConnectionError("Connection closed by server during stream")
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    msg_type = msg.get("type")
                    if msg_type == "chunk":
                        chunk = msg.get("chunk") or ""
                        if chunk:
                            yield chunk
                        continue
                    if msg_type == "task_signal":
                        self.last_task_signal = msg
                        cb = self.on_task_signal
                        if callable(cb):
                            try:
                                cb(msg)
                            except Exception:
                                pass
                        continue
                    if msg_type == "done":
                        self.last_model = msg.get("model")
                        self.last_endpoint = msg.get("endpoint")
                        self.last_usage = msg.get("usage") or {}
                        self.last_cost_rub = msg.get("cost_rub", None)
                        self.last_title = msg.get("title") or None
                        self.last_message_stats = msg.get("message_stats") or {}
                        self.last_active_branch = msg.get("active_branch") or None
                        self.last_facts = msg.get("facts") if isinstance(msg.get("facts"), dict) else None
                        self.last_memory_layers = msg.get("memory_layers") if isinstance(msg.get("memory_layers"), dict) else None
                        self.last_token_stats = msg.get("token_stats") if isinstance(msg.get("token_stats"), dict) else {}
                        self.last_profile_info = msg.get("profile_info") if isinstance(msg.get("profile_info"), dict) else {}
                        self.last_mcp_info = msg.get("mcp_info") if isinstance(msg.get("mcp_info"), dict) else {}
                        self.last_tool_events = msg.get("tool_events") if isinstance(msg.get("tool_events"), list) else []
                        break
                    if msg_type == "error":
                        raise RuntimeError(msg.get("message") or "Agent error")
            except Exception:
                await self._close_connection()
                raise

    async def get_task_state(self) -> Dict[str, Any]:
        return {}

    async def generate_task_plan(self, task: str) -> Dict[str, Any]:
        return {}

    async def confirm_task_plan(self) -> Dict[str, Any]:
        return {}

    async def pause_task(self) -> Dict[str, Any]:
        return {}

    async def resume_task(self) -> Dict[str, Any]:
        return {}

    async def next_task_step(self) -> Dict[str, Any]:
        return {}

    async def update_task_progress(self, *, current: str = "", expected_action: str = "", done_item: str = "", step: Optional[int] = None) -> Dict[str, Any]:
        return {}

    async def delete_task(self) -> Dict[str, Any]:
        return {}
