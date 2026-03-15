import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.shared import JsonFileStore


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TelegramBindingsStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.store = JsonFileStore(base_dir)
        self.bindings_path = os.path.join(self.base_dir, "bindings.json")
        self.store.ensure_file(self.bindings_path, {"bindings": []})

    def list_bindings(self) -> List[Dict[str, Any]]:
        payload = self.store.read_json(self.bindings_path, {"bindings": []})
        bindings = payload.get("bindings") if isinstance(payload.get("bindings"), list) else []
        return [item for item in bindings if isinstance(item, dict)]

    def get_binding(self, username: str) -> Optional[Dict[str, Any]]:
        normalized = str(username or "").strip().lstrip("@").lower()
        if not normalized:
            return None
        for binding in self.list_bindings():
            current = str(binding.get("telegram_username") or "").strip().lstrip("@").lower()
            if current == normalized:
                return binding
        return None

    def save_binding(self, binding: Dict[str, Any]) -> Dict[str, Any]:
        normalized = str(binding.get("telegram_username") or "").strip().lstrip("@").lower()
        if not normalized:
            raise ValueError("telegram_username is required")
        bindings = self.list_bindings()
        updated = False
        for index, item in enumerate(bindings):
            current = str(item.get("telegram_username") or "").strip().lstrip("@").lower()
            if current == normalized:
                bindings[index] = binding
                updated = True
                break
        if not updated:
            bindings.append(binding)
        self.store.write_json(self.bindings_path, {"bindings": bindings})
        return binding
