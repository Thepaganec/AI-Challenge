import os
import json
import aiohttp
from typing import AsyncIterator, List, Dict, Optional, Literal

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
        *,
        model: Optional[str] = None,
        endpoint: Literal["chat", "responses"] = "chat",
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:

        selected_model = model or self.model

        messages: List[Dict[str, str]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        if endpoint == "chat":
            url = f"{self.base_url}/chat/completions"

            payload: Dict[str, object] = {
                "model": selected_model,
                "messages": messages,
                "max_completion_tokens": max_tokens,
                "stream": True,
            }

            # temperature отправляем только если явно задана и != 1
            if temperature is not None and float(temperature) != 1.0:
                payload["temperature"] = float(temperature)

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

        else:
            url = f"{self.base_url}/responses"

            payload_r: Dict[str, object] = {
                "model": selected_model,
                "input": [{"role": "user", "content": user_text}],
                "stream": True,
                "max_output_tokens": max_tokens,
            }

            if temperature is not None and float(temperature) != 1.0:
                payload_r["temperature"] = float(temperature)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload_r) as resp:

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
                            if obj.get("type") == "response.output_text.delta":
                                delta_text = obj.get("delta")
                                if delta_text:
                                    yield delta_text
                        except Exception:
                            continue


