import os
from typing import Any, Dict, Optional

import aiohttp


class TelegramApiClient:
    def __init__(self, bot_token: Optional[str] = None, timeout_sec: int = 20):
        self.bot_token = str(bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.timeout_sec = timeout_sec
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""

    async def _request(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.base_url}/{method}", json=payload or {}) as resp:
                data = await resp.json(content_type=None)
        if not bool(data.get("ok")):
            raise RuntimeError(str(data.get("description") or f"Telegram API error in {method}"))
        return data

    async def get_updates(self, *, limit: int = 100) -> Dict[str, Any]:
        return await self._request("getUpdates", {"limit": int(limit), "timeout": 0})

    async def send_message(self, *, chat_id: str, text: str) -> Dict[str, Any]:
        return await self._request("sendMessage", {"chat_id": str(chat_id), "text": str(text)})
