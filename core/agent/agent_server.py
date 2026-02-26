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
        history = session.get("history") or {}
        if not isinstance(history, dict):
            return []

        out = []
        try:
            keys = sorted(history.keys(), key=lambda x: int(x))
        except Exception:
            keys = list(history.keys())

        for k in keys:
            turn = history.get(k) or {}
            user_text = turn.get("user_text")
            assistant_text = turn.get("assistant_text")

            if isinstance(user_text, str) and user_text:
                out.append({"role": "user", "content": user_text})
            if isinstance(assistant_text, str) and assistant_text:
                out.append({"role": "assistant", "content": assistant_text})

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

                # удаляем файл полностью
                self.memory_store.delete_session_file(session_id)
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

            history = session.get("history") or {}
            if not isinstance(history, dict):
                history = {}

            # история для LLM до добавления текущего сообщения
            session["history"] = history
            history_for_llm = self._history_for_llm(session)

            # turn_id
            try:
                last_idx = max([int(k) for k in history.keys()] or [0])
            except Exception:
                last_idx = 0
            turn_id = str(last_idx + 1)

            # r_prev_prompt_total (из предыдущего turn)
            r_prev_prompt_total = 0
            if last_idx > 0:
                prev_turn = history.get(str(last_idx)) or {}
                r_prev_prompt_total = int(prev_turn.get("r_prompt_total") or 0)

            # сохраняем turn с user_text заранее
            history[turn_id] = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "user_text": user_text,
                "assistant_text": "",
                "model": model,
                "endpoint": endpoint,
                "max_tokens": int(max_tokens),
                "temperature": temperature,
                "usage": {},
                "cost_rub": None,
                "r_prompt_total": 0,
                "c_completion": 0,
                "total_tokens_call": 0,
                "r_prev_prompt_total": int(r_prev_prompt_total),
                "current_message_tokens": 0,
            }

            session["history"] = history
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
                gen = self.gpt.stream_chat(
                    user_text=user_text,
                    system_text=None,
                    history=history_for_llm,
                    max_tokens=max_tokens,
                    model=model,
                    endpoint=endpoint,
                    temperature=temperature,
                    include_usage=True,
                )

                async for chunk in gen:
                    assistant_answer += chunk
                    await self._send_json(writer, {"type": "chunk", "chunk": chunk})

                usage = getattr(self.gpt, "last_usage", None) or {}
                cost_rub = self._calc_cost_rub(model_id=model, usage=usage)

                r = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                c = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                total_call = int(usage.get("total_tokens") or (r + c))

                # то, что тебе нужно:
                current_message_tokens = int(max(r - int(r_prev_prompt_total), 0) + c)

                history[turn_id]["assistant_text"] = assistant_answer
                history[turn_id]["usage"] = usage
                history[turn_id]["cost_rub"] = cost_rub
                history[turn_id]["r_prompt_total"] = int(r)
                history[turn_id]["c_completion"] = int(c)
                history[turn_id]["total_tokens_call"] = int(total_call)
                history[turn_id]["r_prev_prompt_total"] = int(r_prev_prompt_total)
                history[turn_id]["current_message_tokens"] = int(current_message_tokens)

                session["history"] = history
                session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.memory_store.save_session(session)

                message_stats = {
                    "turn_id": turn_id,
                    "r_prompt_total": int(r),
                    "r_prev_prompt_total": int(r_prev_prompt_total),
                    "c_completion": int(c),
                    "current_message_tokens": int(current_message_tokens),
                    "total_tokens_call": int(total_call),
                    "cost_rub": cost_rub,
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
                        "message_stats": message_stats,
                    },
                )

            finally:
                if gen is not None:
                    try:
                        await gen.aclose()
                    except Exception:
                        pass

        except Exception as e:
            tb = traceback.format_exc()
            msg = str(e) or "Unknown error"
            self.logger.write("ERROR", "Ошибка обработки клиента", extra=msg)
            self.logger.write("ERROR", "TRACEBACK", extra=tb)
            await self._send_json(writer, {"type": "error", "message": msg})

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