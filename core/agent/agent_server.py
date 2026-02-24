import sys
sys.dont_write_bytecode = True

import asyncio
import json
import os
import traceback
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv(override=True)

from core.api.gptmodel import GPTModel
from core.agent.agent_logger import AgentFileLogger
from core.agent.memory_store import AgentMemoryStore


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
        self.logger = AgentFileLogger(logs_dir=self.base_dir, prefix="agentlogs")
        self.logger.cleanup_old_logs(keep_days=3)

        self.memory_dir = os.path.join(self.base_dir, "memory")
        self.memory_store = AgentMemoryStore(base_dir=self.memory_dir)

        self.gpt = GPTModel(api_key_env=api_key_env, base_url=base_url, timeout_sec=timeout_sec)

        self.pricing_cache: Dict[str, Dict[str, float]] = {}

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

    def _history_for_llm(self, session: dict) -> list:
        messages = session.get("messages") or []
        if not isinstance(messages, list):
            return []
        out = []
        for m in messages:
            role = (m.get("role") or "").strip()
            content = m.get("content")
            if role and isinstance(content, str):
                out.append({"role": role, "content": content})
        return out

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.logger.write("INFO", "Клиент подключился", extra=str(peer))

        try:
            line = await reader.readline()
            if not line:
                return

            try:
                request = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                await self._send_json(writer, {"type": "error", "message": "Invalid JSON"})
                return

            action = request.get("action")

            if action == "ping":
                await self._send_json(writer, {"type": "pong"})
                return

            if action == "list_sessions":
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
                return

            if action == "get_session":
                session_id = (request.get("session_id") or "").strip()
                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return

                session = self.memory_store.load_session(session_id)
                await self._send_json(writer, {"type": "session", "session": session})
                return

            if action == "reset_session":
                session_id = (request.get("session_id") or "").strip()
                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return

                session = self.memory_store.load_session(session_id)
                session["messages"] = []
                session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.memory_store.save_session(session)

                await self._send_json(writer, {"type": "ok"})
                return

            if action != "stream_chat":
                await self._send_json(writer, {"type": "error", "message": "Unknown action"})
                return

            user_text = (request.get("user_text") or "").strip()
            session_id = (request.get("session_id") or "").strip()

            if not session_id:
                await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                return
            if not user_text:
                await self._send_json(writer, {"type": "error", "message": "Empty user_text"})
                return

            model = (request.get("model") or "").strip() or self.gpt.model
            endpoint = request.get("endpoint") or "chat"
            max_tokens = int(request.get("max_tokens") or 512)

            temperature = request.get("temperature", None)
            if temperature is not None:
                try:
                    temperature = float(temperature)
                except Exception:
                    temperature = None

            session = self.memory_store.load_session(session_id)
            self.memory_store.set_title_if_empty(session, user_text)

            messages = session.get("messages") or []
            if not isinstance(messages, list):
                messages = []

            messages.append(
                {
                    "role": "user",
                    "content": user_text,
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            session["messages"] = messages
            session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.memory_store.save_session(session)

            self.logger.write(
                "INFO",
                "Запрос к LLM",
                extra=f"session_id={session_id} title={session.get('title','')} model={model} endpoint={endpoint}",
            )

            gen = None
            assistant_answer = ""

            try:
                history_for_llm = self._history_for_llm(session)

                gen = self.gpt.stream_chat(
                    user_text=user_text,
                    system_text=None,
                    history=history_for_llm,
                    max_tokens=max_tokens,
                    model=model,
                    endpoint=endpoint,  # Literal["chat","responses"] у твоего GPTModel
                    temperature=temperature,
                    include_usage=True,
                )

                async for chunk in gen:
                    assistant_answer += chunk
                    await self._send_json(writer, {"type": "chunk", "chunk": chunk})

                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_answer,
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                session["messages"] = messages
                session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.memory_store.save_session(session)

                usage = getattr(self.gpt, "last_usage", None) or {}
                cost_rub = self._calc_cost_rub(model_id=model, usage=usage)

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
                    },
                )

                self.logger.write("SUCCESS", "Ответ сформирован", extra=f"session_id={session_id} cost_rub={cost_rub}")

            finally:
                if gen is not None:
                    try:
                        await gen.aclose()
                    except Exception:
                        pass

        except Exception as e:
            tb = traceback.format_exc()
            self.logger.write("ERROR", "Ошибка обработки клиента", extra=str(e))
            self.logger.write("ERROR", "TRACEBACK", extra=tb)
            try:
                await self._send_json(writer, {"type": "error", "message": str(e)})
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