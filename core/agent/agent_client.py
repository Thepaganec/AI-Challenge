import asyncio
import json, sys
sys.dont_write_bytecode = True

from typing import Any, AsyncIterator, Dict, Optional, List
from core.logger.advanced_logger import Logger
from PySide6.QtCore import Signal, QObject


class AgentClient(QObject):

    agent_connection_established = Signal()
    sessions_list_updated = Signal()

    def __init__(self, logger: Logger, host: str = "127.0.0.1", port: int = 8765, timeout_sec: int = 10):
        super().__init__()

        self.logger = logger
        self.host = host
        self.port = port
        self.timeout_sec = timeout_sec

        self.last_usage: Dict[str, Any] = {}
        self.last_cost_rub: Optional[float] = None
        self.last_model: Optional[str] = None
        self.last_endpoint: Optional[str] = None
        self.last_title: Optional[str] = None

        self.last_message_stats: Dict[str, Any] = {}

        self.is_connected = False
        self.agent_watchdog_task = None
        self.sessions_list = []

        # --- агент: вечный watchdog переподключения
        self.agent_watchdog_task = asyncio.get_event_loop().create_task(self.agent_connection_watchdog())

        self.agent_connection_established.connect(
            lambda: asyncio.get_event_loop().create_task(self.get_list_sessions())
        )

    async def connect_to_server(self):
        try:
            self.logger.info("Попытка подключения к серверу...")
            state = await self.ping()
            
            if state:
                self.is_connected = True
                self.logger.success("Сервер ONLINE: соединение установлено")
                self.agent_connection_established.emit()
            else:
                self.is_connected = False
                self.logger.warning("Сервер недоступен. Проверьте запуск сервера.")
        except Exception as e:
            self.is_connected = False
            self.logger.warning(f"Ошибка при попытке связи: {e}")
        
    async def agent_connection_watchdog(self):
        await self.connect_to_server()

        while True:
            await asyncio.sleep(5)
            try:
                state = await self.ping()
                
                if not state:
                    if self.is_connected:
                        self.logger.warning("Связь с сервером потеряна!")
                    
                    self.is_connected = False
                    await self.connect_to_server() 
                else:
                    if not self.is_connected: 
                        self.is_connected = True
                        self.logger.success("Соединение восстановлено")
                        self.agent_connection_established.emit()
                        
            except Exception as e:
                self.logger.error(f"Критическая ошибка в watchdog: {e}")

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

    async def get_list_sessions(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write((json.dumps({"action": "list_sessions"}, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        try:
            line = await reader.readline()
            if not line:
                self.sessions_list = []

            msg = json.loads(line.decode("utf-8", errors="replace"))
            self.sessions_list = []
            if msg.get("type") == "sessions":
                self.sessions_list = msg.get("sessions") or []
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            
        self.sessions_list_updated.emit()

    async def get_session(self, session_id: str) -> Optional[dict]:
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=20_000_000)

        writer.write(
            (json.dumps({"action": "get_session", "session_id": session_id}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await writer.drain()

        try:
            line = await reader.readline()
            if not line:
                return None

            msg = json.loads(line.decode("utf-8", errors="replace"))

            # обычный короткий ответ
            if msg.get("type") == "session":
                return msg.get("session")
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("message") or "Agent error")

            # chunked ответ (если сервер уже умеет)
            if msg.get("type") == "chunked_start" and msg.get("orig_type") == "session":
                chunks = int(msg.get("chunks") or 0)
                parts = [""] * chunks

                while True:
                    line2 = await reader.readline()
                    if not line2:
                        break

                    m2 = json.loads(line2.decode("utf-8", errors="replace"))
                    t = m2.get("type")

                    if t == "chunked_part" and m2.get("orig_type") == "session":
                        i = int(m2.get("i") or 0)
                        data = m2.get("data") or ""
                        if 0 <= i < chunks:
                            parts[i] = data
                        continue

                    if t == "chunked_end" and m2.get("orig_type") == "session":
                        break

                    if t == "error":
                        raise RuntimeError(m2.get("message") or "Agent error")

                full_text = "".join(parts)
                payload = json.loads(full_text)

                if payload.get("type") == "session":
                    return payload.get("session")
                if payload.get("type") == "error":
                    raise RuntimeError(payload.get("message") or "Agent error")
                return None

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
        char_limit: int,
        keep_last_n: int,
        summary_model: str,
        summary_endpoint: str,
    ) -> AsyncIterator[str]:
        self.last_usage = {}
        self.last_cost_rub = None
        self.last_model = None
        self.last_endpoint = None
        self.last_title = None
        self.last_message_stats = {}

        reader, writer = await asyncio.open_connection(self.host, self.port)

        request = {
            "action": "stream_chat",
            "session_id": session_id,
            "user_text": user_text,
            "model": model,
            "endpoint": endpoint,
            "max_tokens": int(max_tokens),
            "temperature": temperature,

            # NEW: параметры контроля длины и суммаризации
            "char_limit": int(char_limit),
            "keep_last_n": int(keep_last_n),
            "summary_model": str(summary_model or "").strip(),
            "summary_endpoint": str(summary_endpoint or "chat"),
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
                    break

                if msg_type == "error":
                    raise RuntimeError(msg.get("message") or "Agent error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass