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

    async def ping(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout_sec,
            )
            writer.write((json.dumps({"action": "ping"}) + "\n").encode("utf-8"))
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_sec)

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            if not line:
                return False

            data = json.loads(line.decode("utf-8", errors="replace"))
            return data.get("type") == "pong"
        except Exception:
            return False

    async def list_sessions(self) -> List[dict]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write((json.dumps({"action": "list_sessions"}, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        try:
            line = await reader.readline()
            if not line:
                return []

            msg = json.loads(line.decode("utf-8", errors="replace"))
            if msg.get("type") == "sessions":
                return msg.get("sessions") or []
            return []
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def get_session(self, session_id: str) -> Optional[dict]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write(
            (json.dumps({"action": "get_session", "session_id": session_id}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await writer.drain()

        try:
            line = await reader.readline()
            if not line:
                return None

            msg = json.loads(line.decode("utf-8", errors="replace"))
            if msg.get("type") == "session":
                return msg.get("session")
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("message") or "Agent error")
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def reset_session(self, session_id: str) -> bool:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write(
            (json.dumps({"action": "reset_session", "session_id": session_id}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await writer.drain()

        try:
            line = await reader.readline()
            if not line:
                return False

            msg = json.loads(line.decode("utf-8", errors="replace"))
            return msg.get("type") == "ok"
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def stream_chat(
        self,
        user_text: str,
        model: str,
        endpoint: str,
        max_tokens: int,
        temperature: Optional[float],
        session_id: str,
    ) -> AsyncIterator[str]:
        self.last_usage = {}
        self.last_cost_rub = None
        self.last_model = None
        self.last_endpoint = None
        self.last_title = None

        reader, writer = await asyncio.open_connection(self.host, self.port)

        request = {
            "action": "stream_chat",
            "session_id": session_id,
            "user_text": user_text,
            "model": model,
            "endpoint": endpoint,
            "max_tokens": int(max_tokens),
            "temperature": temperature,
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
                    break

                if msg_type == "error":
                    raise RuntimeError(msg.get("message") or "Agent error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass