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

    async def _send_json_maybe_chunked(self, writer: asyncio.StreamWriter, payload: Dict[str, Any], *, max_line_bytes: int = 60000) -> None:
        """
        Если payload слишком большой для одной строки readline() у клиента — шлём в несколько строк.

        Протокол:
        1) {"type":"chunked_start","orig_type":"session","chunks":N}
        2) N строк {"type":"chunked_part","orig_type":"session","i":0..N-1,"data":"..."}
        3) {"type":"chunked_end","orig_type":"session"}
        """
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

    async def _summarize_history_text(
        self,
        *,
        history_text: str,
        model: str,
        endpoint: str,
    ) -> str:
        """
        Делает суммаризацию истории через GPTModel.stream_chat (стрим), собирает в строку.
        """
        prompt = (
            "Сожми историю диалога в компактную выжимку, сохранив смысл, факты, договорённости и контекст.\n"
            "Требования:\n"
            "- Пиши по-русски.\n"
            "- Без воды.\n"
            "- Сохраняй имена переменных/методов/классов как есть.\n"
            "- Если есть требования/правила — вынеси их отдельным списком.\n\n"
            "ИСТОРИЯ:\n"
            f"{history_text}"
        )

        out = ""
        gen = self.gpt.stream_chat(
            user_text=prompt,
            system_text=None,
            history=None,
            max_tokens=700,
            model=model,
            endpoint=endpoint,
            temperature=None,
            include_usage=False,
        )
        try:
            async for ch in gen:
                out += ch
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass

        return out.strip()

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

                # ВАЖНО: сессия может быть огромной -> шлём chunked, чтобы readline() у клиента не падал
                await self._send_json_maybe_chunked(writer, {"type": "session", "session": session})
                return

            if action == "reset_session":
                session_id = (request.get("session_id") or "").strip()
                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return

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

            # NEW: параметры контроля длины и суммаризации
            try:
                char_limit = int(request.get("char_limit") or 12000)
            except Exception:
                char_limit = 12000

            try:
                keep_last_n = int(request.get("keep_last_n") or 8)
            except Exception:
                keep_last_n = 8

            summary_model = (request.get("summary_model") or "").strip() or model
            summary_endpoint = (request.get("summary_endpoint") or "").strip() or "chat"

            session = self.memory_store.load_session(session_id)
            self.memory_store.set_title_if_empty(session, user_text)

            history = session.get("history") or {}
            if not isinstance(history, dict):
                history = {}

            # подтянем прошлую суммаризацию
            history_summary = session.get("history_summary") or ""
            if not isinstance(history_summary, str):
                history_summary = ""

            # история для LLM (полная) до добавления текущего сообщения
            session["history"] = history
            history_for_llm_full = self._history_for_llm(session)

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
            session["history_summary"] = history_summary
            self.memory_store.save_session(session)

            gen = None
            assistant_answer = ""

            # ====== NEW_MESSAGE сборка на сервере ======
            # New_message (для измерения длины):
            # HISTORY_SUMMARY + последние N сообщений + NEW_MESSAGE(user_text)
            msgs_flat = list(history_for_llm_full)

            if keep_last_n > 0:
                tail_msgs = msgs_flat[-keep_last_n:]
                old_msgs = msgs_flat[:-keep_last_n]
            else:
                tail_msgs = []
                old_msgs = msgs_flat

            def _to_text(msgs):
                out = []
                for m in msgs:
                    role = m.get("role")
                    content = m.get("content")
                    if not content:
                        continue
                    if role == "user":
                        out.append("USER: " + str(content))
                    elif role == "assistant":
                        out.append("ASSISTANT: " + str(content))
                    else:
                        out.append(str(content))
                return "\n".join(out).strip()

            old_text = _to_text(old_msgs)
            tail_text = _to_text(tail_msgs)

            def _build_new_message_preview(summary_text: str) -> str:
                s = ""
                if isinstance(summary_text, str) and summary_text.strip():
                    s += "HISTORY_SUMMARY:\n" + summary_text.strip() + "\n\n"
                if tail_text:
                    s += "LAST_MESSAGES:\n" + tail_text + "\n\n"
                s += "NEW_MESSAGE:\n" + user_text
                return s

            new_message_preview = _build_new_message_preview(history_summary)
            new_message_len = len(new_message_preview)

            history_summarized = False

            # Если превышаем порог — суммаризируем old_text и сохраняем history_summary
            if char_limit > 0 and new_message_len > char_limit:
                if old_text.strip():
                    try:
                        self.logger.write(
                            "INFO",
                            "История превышает порог, делаю суммаризацию",
                            extra=f"len={new_message_len}/{char_limit}",
                        )

                        # ВАЖНО: метод _summarize_history_text должен быть добавлен в класс LLMAgentServer
                        new_summary = await self._summarize_history_text(
                            history_text=old_text,
                            model=summary_model,
                            endpoint=summary_endpoint,
                        )

                        if isinstance(new_summary, str) and new_summary.strip():
                            history_summary = new_summary.strip()
                            session["history_summary"] = history_summary
                            session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            self.memory_store.save_session(session)
                            history_summarized = True

                            # пересчёт длины после обновления summary
                            new_message_len = len(_build_new_message_preview(history_summary))

                    except Exception as e:
                        self.logger.write("WARN", "Суммаризация не удалась", extra=str(e))

            # ====== Формируем запрос для GPT ======
            system_text = None
            if isinstance(history_summary, str) and history_summary.strip():
                system_text = "History summary (compressed context):\n" + history_summary.strip()

            # В историю для LLM кладём только хвост последних сообщений
            history_for_llm = tail_msgs

            try:
                gen = self.gpt.stream_chat(
                    user_text=user_text,
                    system_text=system_text,
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
                session["history_summary"] = history_summary
                self.memory_store.save_session(session)

                message_stats = {
                    "turn_id": turn_id,
                    "r_prompt_total": int(r),
                    "r_prev_prompt_total": int(r_prev_prompt_total),
                    "c_completion": int(c),
                    "current_message_tokens": int(current_message_tokens),
                    "total_tokens_call": int(total_call),
                    "cost_rub": cost_rub,

                    # NEW: для UI
                    "new_message_len": int(new_message_len),
                    "char_limit": int(char_limit),
                    "history_summarized": bool(history_summarized),
                    "history_summary": history_summary,
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