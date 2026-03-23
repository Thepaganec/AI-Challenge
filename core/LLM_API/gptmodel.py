import asyncio
import json
import os
import re
import time
import inspect
from html import unescape
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import aiohttp


class GPTModel:
    def __init__(
        self,
        api_key_env: str = "PROXYAPI_KEY",
        base_url: str = "https://openai.api.proxyapi.ru/v1",
        model: str = "gpt-5.2-chat-latest",
        timeout_sec: int = 60,
        logger: Optional[Any] = None,
        api_key_optional: bool = False,
        use_max_completion_tokens: bool = True,
        include_stream_options: bool = True,
    ):
        self.api_key = str(os.getenv(api_key_env) or "").strip() if api_key_env else ""
        if (not self.api_key) and (not bool(api_key_optional)):
            raise RuntimeError(
                f"Не найден API ключ в env переменной {api_key_env}. "
                f"Добавь в .env: {api_key_env}=..."
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.logger = logger
        self.api_key_optional = bool(api_key_optional)
        self.use_max_completion_tokens = bool(use_max_completion_tokens)
        self.include_stream_options = bool(include_stream_options)
        self.last_usage: Dict[str, Any] = {}
        self.last_message: Dict[str, Any] = {}
        self.last_finish_reason: str = ""

    def _mask_api_key(self, value: str) -> str:
        token = str(value or "")
        if len(token) <= 10:
            return "***"
        return token[:6] + "***" + token[-4:]

    def _log_struct(self, level: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if self.logger is None:
            return
        extra = None
        if isinstance(payload, dict):
            extra = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            self.logger.write(level, message, extra=extra)
        except Exception:
            pass

    async def get_pricing_rub_per_1m(self) -> Dict[str, Dict[str, float]]:
        if not hasattr(self, "_pricing_cache"):
            self._pricing_cache = None
        if isinstance(self._pricing_cache, dict) and self._pricing_cache:
            return self._pricing_cache

        url = "https://proxyapi.ru/pricing/list"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"}
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"ProxyAPI pricing fetch error: HTTP {resp.status}\n{await resp.text()}")
                html = await resp.text()

        html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
        html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)

        def _strip_tags(s: str) -> str:
            s = re.sub(r"(?is)<[^>]+>", " ", s)
            s = unescape(s).replace("\xa0", " ")
            return re.sub(r"\s+", " ", s).strip()

        def _parse_rub_number(s: str) -> Optional[float]:
            m = re.search(r"([0-9][0-9\s]*([.,][0-9]+)?)\s*₽", s)
            if not m:
                return None
            num = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
            try:
                return float(num)
            except Exception:
                return None

        pricing: Dict[str, Dict[str, float]] = {}
        rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", html)
        for row_html in rows:
            tds = re.findall(r"(?is)<td\b[^>]*>.*?</td>", row_html)
            if len(tds) < 3:
                continue
            cells = [_strip_tags(td) for td in tds]
            if len(cells) < 3:
                continue
            model_id = cells[1]
            prices_blob = " | ".join(cells[2:])
            m_in = re.search(r"Ввод\s*:\s*([^|]+)", prices_blob)
            m_out = re.search(r"Вывод\s*:\s*([^|]+)", prices_blob)
            in_price = _parse_rub_number(m_in.group(1)) if m_in else None
            out_price = _parse_rub_number(m_out.group(1)) if m_out else None
            if in_price is not None and out_price is not None:
                pricing[model_id] = {"in": float(in_price), "out": float(out_price)}
        self._pricing_cache = pricing
        return pricing

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _sanitize_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            if not role:
                continue
            entry: Dict[str, Any] = {"role": role}
            if "content" in item:
                entry["content"] = item.get("content")
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                entry["tool_calls"] = item.get("tool_calls")
            if role == "tool":
                tool_call_id = str(item.get("tool_call_id") or "").strip()
                if tool_call_id:
                    entry["tool_call_id"] = tool_call_id
            sanitized.append(entry)
        return sanitized

    async def _stream_chat_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        trace_id: Optional[str] = None,
        on_text_chunk: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Any]:
        selected_model = model or self.model
        payload: Dict[str, Any] = {
            "model": selected_model,
            "messages": self._sanitize_messages(messages),
            "stream": True,
        }
        if self.use_max_completion_tokens:
            payload["max_completion_tokens"] = int(max_tokens)
        else:
            payload["max_tokens"] = int(max_tokens)
        if self.include_stream_options:
            payload["stream_options"] = {"include_usage": True}
        if temperature is not None and float(temperature) != 1.0:
            payload["temperature"] = float(temperature)
        if isinstance(tools, list) and tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        url = f"{self.base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        t0 = time.perf_counter()
        chars_out = 0
        content_parts: List[str] = []
        preview_parts: List[str] = []
        self.last_usage = {}
        finish_reason = ""
        role = "assistant"
        tool_calls_by_index: Dict[int, Dict[str, Any]] = {}

        self._log_struct(
            "INFO",
            "GPTMODEL_API_REQUEST",
            {
                "req_id": trace_id,
                "endpoint": "chat_stream",
                "url": url,
                "headers": {
                    "Content-Type": "application/json",
                },
                "payload": payload,
            },
        )
        if self.api_key:
            self._log_struct(
                "INFO",
                "GPTMODEL_API_REQUEST_AUTH",
                {"req_id": trace_id, "auth": self._mask_api_key(self.api_key)},
            )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                self._log_struct(
                    "INFO",
                    "GPTMODEL_API_RESPONSE_META",
                    {
                        "req_id": trace_id,
                        "endpoint": "chat_stream",
                        "status": int(resp.status),
                        "ok": bool(200 <= int(resp.status) < 300),
                    },
                )
                if resp.status < 200 or resp.status >= 300:
                    body_text = await resp.text()
                    self._log_struct(
                        "ERROR",
                        "GPTMODEL_API_RESPONSE_ERROR",
                        {"req_id": trace_id, "endpoint": "chat_stream", "status": int(resp.status), "body": body_text},
                    )
                    raise RuntimeError(f"LLM API error: HTTP {resp.status}\n{body_text}")

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue

                    if isinstance(obj.get("usage"), dict):
                        self.last_usage = obj.get("usage") or {}

                    choices = obj.get("choices") if isinstance(obj.get("choices"), list) else []
                    if not choices:
                        continue
                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                    finish_reason = str(choice0.get("finish_reason") or finish_reason or "")
                    delta = choice0.get("delta") if isinstance(choice0.get("delta"), dict) else {}
                    if not isinstance(delta, dict):
                        continue

                    delta_role = str(delta.get("role") or "").strip()
                    if delta_role:
                        role = delta_role

                    content = delta.get("content")
                    if content:
                        text_chunk = str(content)
                        chars_out += len(text_chunk)
                        content_parts.append(text_chunk)
                        if len("".join(preview_parts)) < 4000:
                            preview_parts.append(text_chunk)
                        if callable(on_text_chunk):
                            result = on_text_chunk(text_chunk)
                            if inspect.isawaitable(result):
                                await result

                    tool_deltas = delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []
                    for tool_delta in tool_deltas:
                        if not isinstance(tool_delta, dict):
                            continue
                        try:
                            index = int(tool_delta.get("index") or 0)
                        except Exception:
                            index = 0
                        current = tool_calls_by_index.setdefault(
                            index,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        item_id = str(tool_delta.get("id") or "")
                        if item_id:
                            current["id"] = item_id
                        item_type = str(tool_delta.get("type") or "").strip()
                        if item_type:
                            current["type"] = item_type
                        function = tool_delta.get("function") if isinstance(tool_delta.get("function"), dict) else {}
                        function_name = str(function.get("name") or "")
                        if function_name:
                            current["function"]["name"] += function_name
                        function_args = function.get("arguments")
                        if function_args:
                            current["function"]["arguments"] += str(function_args)

        tool_calls: List[Dict[str, Any]] = [tool_calls_by_index[idx] for idx in sorted(tool_calls_by_index.keys())]
        normalized_message: Dict[str, Any] = {
            "role": role or "assistant",
            "content": "".join(content_parts),
        }
        if tool_calls:
            normalized_message["tool_calls"] = tool_calls

        self._log_struct(
            "INFO",
            "GPTMODEL_API_STREAM_DONE",
            {
                "req_id": trace_id,
                "endpoint": "chat_stream",
                "duration_sec": round(time.perf_counter() - t0, 3),
                "chars_out": chars_out,
                "usage": self.last_usage,
                "finish_reason": finish_reason,
                "tool_calls": tool_calls,
                "response_preview": "".join(preview_parts)[:4000],
            },
        )
        return {
            "message": normalized_message,
            "usage": dict(self.last_usage or {}),
            "finish_reason": finish_reason,
        }

    async def stream_chat(
        self,
        user_text: Optional[str] = None,
        system_text: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        max_tokens: int = 512,
        model: Optional[str] = None,
        endpoint: str = "chat",
        temperature: Optional[float] = None,
        include_usage: bool = True,
        trace_id: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> AsyncIterator[str]:
        request_messages: List[Dict[str, Any]] = []
        if isinstance(messages, list) and messages:
            request_messages = [dict(item) for item in messages if isinstance(item, dict)]
        else:
            if system_text:
                request_messages.append({"role": "system", "content": system_text})
            for item in history or []:
                if isinstance(item, dict):
                    request_messages.append(dict(item))
            request_messages.append({"role": "user", "content": user_text or ""})

        self.last_usage = {}
        self.last_message = {}
        self.last_finish_reason = ""

        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        async def _on_chunk(chunk: str) -> None:
            await queue.put(chunk)

        async def _runner() -> None:
            try:
                result = await self._stream_chat_completion(
                    messages=request_messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    trace_id=trace_id,
                    tools=tools,
                    tool_choice=tool_choice,
                    on_text_chunk=_on_chunk,
                )
                self.last_message = result.get("message") if isinstance(result.get("message"), dict) else {}
                self.last_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                self.last_finish_reason = str(result.get("finish_reason") or "")
            finally:
                await queue.put(sentinel)

        task = asyncio.create_task(_runner())
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                yield str(item)
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
