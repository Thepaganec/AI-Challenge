import os
import json
import aiohttp
import re
import time
from html import unescape
from typing import AsyncIterator, List, Dict, Optional, Literal, Any

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

        # Последняя статистика usage по стриму (токены и т.п.)
        self.last_usage: Optional[Dict[str, Any]] = None

    async def get_model_price_rub_per_1m(self, model_id: str) -> Optional[Dict[str, float]]:
        table = await self.get_pricing_rub_per_1m()
        return table.get((model_id or "").strip())
    
    async def get_pricing_rub_per_1m(self) -> Dict[str, Dict[str, float]]:
        """
        Парсит https://proxyapi.ru/pricing/list по таблице (<tr>/<td>) и возвращает:
        {
            "model_id": {"in": <руб за 1M>, "out": <руб за 1M>},
            ...
        }

        Загружается ОДИН раз и кэшируется до перезапуска приложения.
        """
        if not hasattr(self, "_pricing_cache"):
            self._pricing_cache = None

        if isinstance(self._pricing_cache, dict) and self._pricing_cache:
            return self._pricing_cache

        url = "https://proxyapi.ru/pricing/list"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml",
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status < 200 or resp.status >= 300:
                    body_text = await resp.text()
                    raise RuntimeError(f"ProxyAPI pricing fetch error: HTTP {resp.status}\n{body_text}")

                html = await resp.text()

        # Убираем script/style чтобы не мешали
        html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
        html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)

        def _strip_tags(s: str) -> str:
            s = re.sub(r"(?is)<[^>]+>", " ", s)
            s = unescape(s)
            s = s.replace("\xa0", " ")
            return re.sub(r"\s+", " ", s).strip()

        def _parse_rub_number(s: str) -> Optional[float]:
            """
            Вытаскивает число перед ₽, поддерживает пробелы и запятую:
            'Ввод: 129 ₽ за 1M токенов' -> 129.0
            '2 577 ₽' -> 2577.0
            '7 895,00 ₽' -> 7895.0
            """
            m = re.search(r"([0-9][0-9\s]*([.,][0-9]+)?)\s*₽", s)
            if not m:
                return None
            num = m.group(1).replace(" ", "").replace("\xa0", "")
            num = num.replace(",", ".")
            try:
                return float(num)
            except Exception:
                return None

        pricing: Dict[str, Dict[str, float]] = {}

        # Находим строки таблицы
        rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", html)

        for row_html in rows:
            # Берём ячейки
            tds = re.findall(r"(?is)<td\b[^>]*>.*?</td>", row_html)
            if len(tds) < 3:
                continue

            cells = [_strip_tags(td) for td in tds]
            if not cells:
                continue

            # По факту у ProxyAPI в строке обычно:
            # 0: Provider (OpenAI)
            # 1: Model (gpt-3.5-turbo)
            # 2..: Тарифы (ввод/вывод) текстом
            provider = cells[0]
            model_id = cells[1] if len(cells) > 1 else ""

            if not provider or not model_id:
                continue

            # Склеим остальные колонки (там и "Ввод", и "Вывод")
            prices_blob = " | ".join(cells[2:])

            # Ищем "Ввод:" и "Вывод:" (иногда они в разных td, но мы уже склеили)
            in_price = None
            out_price = None

            m_in = re.search(r"Ввод\s*:\s*([^|]+)", prices_blob)
            if m_in:
                in_price = _parse_rub_number(m_in.group(1))

            m_out = re.search(r"Вывод\s*:\s*([^|]+)", prices_blob)
            if m_out:
                out_price = _parse_rub_number(m_out.group(1))

            # Иногда "Ввод" / "Вывод" могут быть без двоеточия, подстрахуемся:
            if in_price is None:
                m_in2 = re.search(r"Ввод\s*([0-9][0-9\s]*([.,][0-9]+)?)\s*₽", prices_blob)
                if m_in2:
                    in_price = _parse_rub_number(m_in2.group(0))

            if out_price is None:
                m_out2 = re.search(r"Вывод\s*([0-9][0-9\s]*([.,][0-9]+)?)\s*₽", prices_blob)
                if m_out2:
                    out_price = _parse_rub_number(m_out2.group(0))

            if in_price is not None and out_price is not None:
                pricing[model_id] = {"in": float(in_price), "out": float(out_price)}

        self._pricing_cache = pricing
        return pricing

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
        include_usage: bool = True,
    ) -> AsyncIterator[str]:

        selected_model = model or self.model
        self.last_usage = None  # сбрасываем перед каждым запросом

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

            # Просим usage в конце стрима, чтобы замерять токены
            if include_usage:
                payload["stream_options"] = {"include_usage": True}

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

                        # usage обычно приходит в одном из финальных чанков, если include_usage=True
                        try:
                            usage = obj.get("usage")
                            if isinstance(usage, dict):
                                self.last_usage = usage
                        except Exception:
                            pass

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

                        # В Responses usage часто прилетает в событии завершения.
                        # Мы не завязываемся на точный формат: ловим любой dict usage, где бы он ни был.
                        try:
                            if isinstance(obj.get("usage"), dict):
                                self.last_usage = obj.get("usage")

                            resp_obj = obj.get("response")
                            if isinstance(resp_obj, dict) and isinstance(resp_obj.get("usage"), dict):
                                self.last_usage = resp_obj.get("usage")
                        except Exception:
                            pass

                        try:
                            if obj.get("type") == "response.output_text.delta":
                                delta_text = obj.get("delta")
                                if delta_text:
                                    yield delta_text
                        except Exception:
                            continue


