import json
import os
import re
import time
from html import unescape
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp


class GPTModel:
    def __init__(
        self,
        api_key_env: str = "PROXYAPI_KEY",
        base_url: str = "https://openai.api.proxyapi.ru/v1",
        model: str = "gpt-5.2-chat-latest",
        timeout_sec: int = 60,
        logger: Optional[Any] = None,
    ):
        self.api_key = os.getenv(api_key_env)
        if not self.api_key:
            raise RuntimeError(
                f"Не найден API ключ в env переменной {api_key_env}. "
                f"Добавь в .env: {api_key_env}=..."
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.logger = logger
        self.last_usage: Dict[str, Any] = {}

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
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

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

    async def chat_completion(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        selected_model = model or self.model
        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": selected_model,
            "messages": self._sanitize_messages(messages),
            "stream": False,
            "max_completion_tokens": int(max_tokens),
        }
        if temperature is not None and float(temperature) != 1.0:
            payload["temperature"] = float(temperature)
        if isinstance(tools, list) and tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        t0 = time.perf_counter()
        self._log_struct(
            "INFO",
            "GPTMODEL_API_REQUEST",
            {
                "req_id": trace_id,
                "endpoint": "chat",
                "url": url,
                "headers": {
                    "Authorization": f"Bearer {self._mask_api_key(self.api_key)}",
                    "Content-Type": "application/json",
                },
                "payload": payload,
            },
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                body_text = await resp.text()
                self._log_struct(
                    "INFO",
                    "GPTMODEL_API_RESPONSE_META",
                    {
                        "req_id": trace_id,
                        "endpoint": "chat",
                        "status": int(resp.status),
                        "ok": bool(200 <= int(resp.status) < 300),
                    },
                )
                if resp.status < 200 or resp.status >= 300:
                    self._log_struct(
                        "ERROR",
                        "GPTMODEL_API_RESPONSE_ERROR",
                        {"req_id": trace_id, "endpoint": "chat", "status": int(resp.status), "body": body_text},
                    )
                    raise RuntimeError(f"ProxyAPI error: HTTP {resp.status}\n{body_text}")
                data = json.loads(body_text)

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self.last_usage = usage
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = {}
        finish_reason = ""
        if choices:
            choice0 = choices[0] if isinstance(choices[0], dict) else {}
            message = choice0.get("message") if isinstance(choice0.get("message"), dict) else {}
            finish_reason = str(choice0.get("finish_reason") or "")

        normalized_message = {
            "role": str(message.get("role") or "assistant"),
            "content": message.get("content"),
        }
        if isinstance(message.get("tool_calls"), list):
            normalized_message["tool_calls"] = message.get("tool_calls")

        self._log_struct(
            "INFO",
            "GPTMODEL_API_RESPONSE",
            {
                "req_id": trace_id,
                "endpoint": "chat",
                "duration_sec": round(time.perf_counter() - t0, 3),
                "usage": usage,
                "finish_reason": finish_reason,
                "message": normalized_message,
                "reasoning": data.get("reasoning"),
            },
        )
        return {
            "message": normalized_message,
            "usage": usage,
            "finish_reason": finish_reason,
            "raw": data,
        }

    async def complete_text(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = await self.chat_completion(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            trace_id=trace_id,
        )
        message = result.get("message") if isinstance(result.get("message"), dict) else {}
        return {
            "text": str(message.get("content") or ""),
            "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
            "raw": result.get("raw"),
        }

    async def stream_messages(
        self,
        *,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        selected_model = model or self.model
        payload: Dict[str, Any] = {
            "model": selected_model,
            "messages": self._sanitize_messages(messages),
            "stream": True,
            "max_completion_tokens": int(max_tokens),
            "stream_options": {"include_usage": True},
        }
        if temperature is not None and float(temperature) != 1.0:
            payload["temperature"] = float(temperature)

        url = f"{self.base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        t0 = time.perf_counter()
        chunks = 0
        chars_out = 0
        preview_parts: List[str] = []
        self.last_usage = {}
        self._log_struct(
            "INFO",
            "GPTMODEL_API_REQUEST",
            {
                "req_id": trace_id,
                "endpoint": "chat_stream",
                "url": url,
                "headers": {
                    "Authorization": f"Bearer {self._mask_api_key(self.api_key)}",
                    "Content-Type": "application/json",
                },
                "payload": payload,
            },
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
                    raise RuntimeError(f"ProxyAPI error: HTTP {resp.status}\n{body_text}")

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
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else {}
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if not content:
                        continue
                    chunks += 1
                    chars_out += len(content)
                    if len("".join(preview_parts)) < 4000:
                        preview_parts.append(content)
                    yield content

        self._log_struct(
            "INFO",
            "GPTMODEL_API_STREAM_DONE",
            {
                "req_id": trace_id,
                "endpoint": "chat_stream",
                "duration_sec": round(time.perf_counter() - t0, 3),
                "chunks": chunks,
                "chars_out": chars_out,
                "usage": self.last_usage,
                "response_preview": "".join(preview_parts)[:4000],
            },
        )

    async def stream_chat(
        self,
        user_text: str,
        system_text: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 512,
        model: Optional[str] = None,
        endpoint: str = "chat",
        temperature: Optional[float] = None,
        include_usage: bool = True,
        trace_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        messages: List[Dict[str, Any]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for item in history or []:
            if isinstance(item, dict):
                messages.append(dict(item))
        messages.append({"role": "user", "content": user_text})
        async for chunk in self.stream_messages(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            trace_id=trace_id,
        ):
            yield chunk
