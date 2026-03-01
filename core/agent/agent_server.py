import sys
sys.dont_write_bytecode = True

import asyncio
import json
import os
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple

from dotenv import load_dotenv
load_dotenv(override=True)

from core.api.gptmodel import GPTModel
from core.agent.agent_logger import AgentFileLogger
from core.agent.memory_store import AgentMemoryStore
from core.agent.strategies import (
    build_sliding_window,
    parse_facts_from_user_text,
    build_facts_strategy,
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
            # если ветки нет — создаём пустую
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            branches[bid] = {
                "branch_id": bid,
                "name": bid,
                "created_at": now,
                "updated_at": now,
                "facts": {},
                "checkpoints": {},
                "history": [],
            }

        session["active_branch"] = bid
        return bid, branches[bid]

    def _branch_history(self, branch: Dict[str, Any]) -> List[Dict[str, str]]:
        h = branch.get("history")
        if not isinstance(h, list):
            h = []
            branch["history"] = h
        out = []
        for m in h:
            role = (m.get("role") or "").strip()
            content = m.get("content")
            if role and content is not None:
                out.append({"role": role, "content": str(content)})
        return out

    def _ensure_title(self, session: Dict[str, Any], user_text: str) -> None:
        self.memory_store.set_title_if_empty(session, user_text)

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

            if action == "create_checkpoint":
                session_id = (request.get("session_id") or "").strip()
                branch_id = (request.get("branch_id") or "").strip() or None
                name = (request.get("name") or "").strip()

                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return

                session = self.memory_store.load_session(session_id)
                bid, branch = self._get_branch(session, branch_id)
                history = self._branch_history(branch)

                cp_id = (name or f"cp_{len(branch.get('checkpoints') or {}) + 1}").strip()
                if not isinstance(branch.get("checkpoints"), dict):
                    branch["checkpoints"] = {}

                branch["checkpoints"][cp_id] = len(history)  # индекс = длина (точка "после последнего сообщения")
                branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                session["updated_at"] = branch["updated_at"]
                session["active_branch"] = bid

                self.memory_store.save_session(session)

                await self._send_json(writer, {"type": "ok", "checkpoint_id": cp_id, "branch_id": bid})
                return

            if action == "create_branch":
                session_id = (request.get("session_id") or "").strip()
                from_branch_id = (request.get("from_branch_id") or "").strip() or None
                checkpoint_id = (request.get("checkpoint_id") or "").strip()
                new_branch_name = (request.get("new_branch_name") or "").strip()

                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return
                if not checkpoint_id:
                    await self._send_json(writer, {"type": "error", "message": "checkpoint_id is required"})
                    return

                session = self.memory_store.load_session(session_id)
                src_bid, src_branch = self._get_branch(session, from_branch_id)
                cps = src_branch.get("checkpoints") or {}
                if not isinstance(cps, dict) or checkpoint_id not in cps:
                    await self._send_json(writer, {"type": "error", "message": "checkpoint not found"})
                    return

                cut = int(cps.get(checkpoint_id) or 0)
                src_history = self._branch_history(src_branch)
                new_history = list(src_history[:cut])

                new_bid = str(uuid.uuid4())[:8]
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                session["branches"][new_bid] = {
                    "branch_id": new_bid,
                    "name": new_branch_name or f"{src_bid}:{checkpoint_id}",
                    "created_at": now,
                    "updated_at": now,
                    "facts": dict(src_branch.get("facts") or {}),
                    "checkpoints": {},
                    "history": new_history,
                }

                session["active_branch"] = new_bid
                session["updated_at"] = now
                self.memory_store.save_session(session)

                await self._send_json(writer, {"type": "ok", "branch_id": new_bid})
                return

            if action == "switch_branch":
                session_id = (request.get("session_id") or "").strip()
                branch_id = (request.get("branch_id") or "").strip()

                if not session_id:
                    await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                    return
                if not branch_id:
                    await self._send_json(writer, {"type": "error", "message": "branch_id is required"})
                    return

                session = self.memory_store.load_session(session_id)
                bid, _ = self._get_branch(session, branch_id)
                session["active_branch"] = bid
                session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.memory_store.save_session(session)

                await self._send_json(writer, {"type": "ok", "active_branch": bid})
                return

            if action != "stream_chat":
                await self._send_json(writer, {"type": "error", "message": "Unknown action"})
                return

            # ===== stream_chat =====
            session_id = (request.get("session_id") or "").strip()
            branch_id = (request.get("branch_id") or "").strip() or None
            user_text = (request.get("user_text") or "").strip()

            if not session_id:
                await self._send_json(writer, {"type": "error", "message": "session_id is required"})
                return
            if not user_text:
                await self._send_json(writer, {"type": "error", "message": "Empty user_text"})
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

            strategy = (request.get("context_strategy") or "sliding").strip().lower()
            try:
                keep_last_n = int(request.get("keep_last_n") or 8)
            except Exception:
                keep_last_n = 8

            # load session + branch
            session = self.memory_store.load_session(session_id)
            self._ensure_title(session, user_text)

            bid, branch = self._get_branch(session, branch_id)
            history = self._branch_history(branch)

            # 1) update facts after each user message (только для facts стратегии, но хранить можно всегда)
            facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
            facts = parse_facts_from_user_text(user_text, facts)
            branch["facts"] = facts

            # 2) append user message
            history.append({"role": "user", "content": user_text})

            # 3) build prompt context based on strategy
            system_text: Optional[str] = None
            history_for_llm: List[Dict[str, str]] = []

            if strategy == "facts":
                system_text, history_for_llm = build_facts_strategy(history[:-1], facts, keep_last_n)  # без текущего user_text
            elif strategy == "branching":
                # branching по сути = sliding, но по ветке
                history_for_llm = build_sliding_window(history[:-1], keep_last_n)
            else:
                # default sliding
                history_for_llm = build_sliding_window(history[:-1], keep_last_n)

            # 4) call model (user_text отдельно)
            gen = None
            assistant_answer = ""

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

                # 5) usage/cost
                usage = getattr(self.gpt, "last_usage", None) or {}
                cost_rub = self._calc_cost_rub(model_id=model, usage=usage)

                # 6) append assistant and save
                history.append({"role": "assistant", "content": assistant_answer})

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                branch["updated_at"] = now
                session["updated_at"] = now
                session["active_branch"] = bid

                self.memory_store.save_session(session)

                # prompt tokens estimation for "sent" messages: system + history_for_llm + user_text
                message_stats = {
                    "strategy": strategy,
                    "branch_id": bid,
                    "keep_last_n": int(keep_last_n),
                    "facts_count": int(len(facts) if isinstance(facts, dict) else 0),
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
                        "facts": facts if strategy == "facts" else None,
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
