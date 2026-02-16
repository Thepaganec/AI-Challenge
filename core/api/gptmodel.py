import os
import json
import aiohttp
from typing import AsyncIterator, List, Dict, Optional

class GPTModel:
    def __init__(
        self,
        api_key_env: str = "PROXYAPI_KEY",
        base_url: str = "https://openai.api.proxyapi.ru/v1",
        model: str = "openai/gpt-5.2-chat-latest",
        timeout_sec: int = 60,
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

    async def stream_chat(
        self,
        user_text: str,
        system_text: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        max_tokens: int = 800,
    ) -> AsyncIterator[str]:

        messages: List[Dict[str, str]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        url = f"{self.base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "stream": True,
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:

                if resp.status < 200 or resp.status >= 300:
                    body_text = await resp.text()
                    raise RuntimeError(
                        f"ProxyAPI error: HTTP {resp.status}\n{body_text}"
                    )

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

                    try:
                        delta = obj["choices"][0]["delta"]
                        chunk = delta.get("content")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue


