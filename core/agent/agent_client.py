import asyncio
import json, sys
sys.dont_write_bytecode = True

from typing import Any, AsyncIterator, Dict, Optional, List


class AgentClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout_sec: int = 10):
        self.host = host
        self.port = port
        self.timeout_sec = timeout_sec

        self.last_usage: Dict[str, Any] = {}
        self.last_cost_rub: Optional[float] = None
        self.last_model: Optional[str] = None
        self.last_endpoint: Optional[str] = None
        self.last_title: Optional[str] = None
        self.last_message_stats: Dict[str, Any] = {}
        self.last_active_branch: Optional[str] = None
        self.last_facts: Optional[dict] = None

    async def _rpc(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=20_000_000)
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_sec)
            if not line:
                return {"type": "error", "message": "Empty response"}

            msg = json.loads(line.decode("utf-8", errors="replace"))

            # chunked response support
            if msg.get("type") == "chunked_start":
                orig = msg.get("orig_type")
                chunks = int(msg.get("chunks") or 0)
                parts = [""] * chunks

                while True:
                    line2 = await reader.readline()
                    if not line2:
                        break
                    m2 = json.loads(line2.decode("utf-8", errors="replace"))
                    t = m2.get("type")

                    if t == "chunked_part" and m2.get("orig_type") == orig:
                        i = int(m2.get("i") or 0)
                        data = m2.get("data") or ""
                        if 0 <= i < chunks:
                            parts[i] = data
                        continue

                    if t == "chunked_end" and m2.get("orig_type") == orig:
                        break

                    if t == "error":
                        return m2

                full_text = "".join(parts)
                return json.loads(full_text)

            return msg
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def ping(self) -> bool:
        try:
            msg = await self._rpc({"action": "ping"})
            return msg.get("type") == "pong"
        except Exception:
            return False

    async def list_sessions(self) -> List[dict]:
        msg = await self._rpc({"action": "list_sessions"})
        if msg.get("type") == "sessions":
            return msg.get("sessions") or []
        return []

    async def get_session(self, session_id: str) -> Optional[dict]:
        msg = await self._rpc({"action": "get_session", "session_id": session_id})
        if msg.get("type") == "session":
            return msg.get("session")
        if msg.get("type") == "error":
            raise RuntimeError(msg.get("message") or "Agent error")
        return None

    async def reset_session(self, session_id: str) -> bool:
        msg = await self._rpc({"action": "reset_session", "session_id": session_id})
        return msg.get("type") == "ok"

    async def create_checkpoint(self, session_id: str, branch_id: str, name: str = "") -> str:
        msg = await self._rpc({
            "action": "create_checkpoint",
            "session_id": session_id,
            "branch_id": branch_id,
            "name": name,
        })

        t = msg.get("type")
        if t == "ok":
            return msg.get("checkpoint_id") or ""

        # backward compatibility (если сервер ещё отдаёт старый тип)
        if t == "checkpoint_created":
            cp = msg.get("checkpoint") if isinstance(msg.get("checkpoint"), dict) else {}
            return str(cp.get("id") or "")

        raise RuntimeError(msg.get("message") or "Agent error")

    async def create_branch(self, session_id: str, from_branch_id: str, checkpoint_id: str, new_branch_name: str = "") -> str:
        msg = await self._rpc({
            "action": "create_branch",
            "session_id": session_id,
            "from_branch_id": from_branch_id,
            "checkpoint_id": checkpoint_id,
            "new_branch_name": new_branch_name,
        })

        t = msg.get("type")
        if t == "ok":
            return msg.get("branch_id") or ""

        # backward compatibility
        if t == "branch_created":
            return msg.get("branch_id") or ""

        raise RuntimeError(msg.get("message") or "Agent error")

    async def create_branch(self, session_id: str, from_branch_id: str, checkpoint_id: str, new_branch_name: str = "") -> str:
        msg = await self._rpc({
            "action": "create_branch",
            "session_id": session_id,
            "from_branch_id": from_branch_id,
            "checkpoint_id": checkpoint_id,
            "new_branch_name": new_branch_name,
        })

        t = msg.get("type")
        if t == "ok":
            return msg.get("branch_id") or ""

        # backward compatibility
        if t == "branch_created":
            return msg.get("branch_id") or ""

        raise RuntimeError(msg.get("message") or "Agent error")

    async def stream_chat(
        self,
        user_text: str,
        model: str,
        endpoint: str,
        max_tokens: int,
        temperature: Optional[float],
        session_id: str,
        branch_id: str,
        keep_last_n: int,
        context_strategy: str,
    ) -> AsyncIterator[str]:
        self.last_usage = {}
        self.last_cost_rub = None
        self.last_model = None
        self.last_endpoint = None
        self.last_title = None
        self.last_message_stats = {}
        self.last_active_branch = None
        self.last_facts = None

        reader, writer = await asyncio.open_connection(self.host, self.port)

        request = {
            "action": "stream_chat",
            "session_id": session_id,
            "branch_id": branch_id,
            "user_text": user_text,
            "model": model,
            "endpoint": endpoint,
            "max_tokens": int(max_tokens),
            "temperature": temperature,
            "keep_last_n": int(keep_last_n),
            "context_strategy": str(context_strategy or "sliding"),
        }

        writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                msg = json.loads(line.decode("utf-8", errors="replace"))
                msg_type = msg.get("type")

                if msg_type == "chunk":
                    chunk = msg.get("chunk") or ""
                    if chunk:
                        yield chunk
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
                    break

                if msg_type == "error":
                    raise RuntimeError(msg.get("message") or "Agent error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
