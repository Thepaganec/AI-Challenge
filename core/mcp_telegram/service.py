from typing import Any, Dict

from .client import TelegramApiClient
from .storage import TelegramBindingsStore, now_iso


class TelegramMCPService:
    def __init__(self, api_client: TelegramApiClient, storage: TelegramBindingsStore, logger: Any):
        self.api_client = api_client
        self.storage = storage
        self.logger = logger

    async def resolve_chat_id(self, username: str, trace_id: str = "") -> Dict[str, Any]:
        normalized = str(username or "").strip().lstrip("@").lower()
        if not normalized:
            raise ValueError("username is required")

        cached = self.storage.get_binding(normalized)
        if cached is not None:
            cached["last_verified_at"] = now_iso()
            self.storage.save_binding(cached)
            return {
                "ok": True,
                "source": "mcp_telegram_cache",
                "binding": cached,
                "chat_id": str(cached.get("chat_id") or ""),
                "telegram_username": normalized,
                "trace_id": trace_id,
            }

        updates = await self.api_client.get_updates(limit=100)
        entries = updates.get("result") if isinstance(updates.get("result"), list) else []
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            candidates = {
                str(from_user.get("username") or "").strip().lstrip("@").lower(),
                str(chat.get("username") or "").strip().lstrip("@").lower(),
            }
            if normalized not in candidates:
                continue
            chat_id = str(chat.get("id") or "").strip()
            if not chat_id:
                continue
            binding = {
                "telegram_username": normalized,
                "chat_id": chat_id,
                "first_seen_at": now_iso(),
                "last_verified_at": now_iso(),
            }
            self.storage.save_binding(binding)
            return {
                "ok": True,
                "source": "telegram_api",
                "binding": binding,
                "chat_id": chat_id,
                "telegram_username": normalized,
                "trace_id": trace_id,
            }

        return {
            "ok": False,
            "is_error": True,
            "error_type": "not_found",
            "message": (
                f"Telegram username @{normalized} not found in bot updates. "
                "Ask the user to send any message to the bot and retry."
            ),
            "telegram_username": normalized,
            "trace_id": trace_id,
        }

    async def send_message(self, chat_id: str, text: str, trace_id: str = "") -> Dict[str, Any]:
        clean_chat_id = str(chat_id or "").strip()
        clean_text = str(text or "")
        if not clean_chat_id:
            raise ValueError("chat_id is required")
        if not clean_text.strip():
            raise ValueError("text is required")

        response = await self.api_client.send_message(chat_id=clean_chat_id, text=clean_text)
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        return {
            "ok": True,
            "trace_id": trace_id,
            "chat_id": clean_chat_id,
            "message_id": result.get("message_id"),
            "telegram_result": result,
        }
