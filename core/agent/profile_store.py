import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AgentProfileStore:
    def __init__(self, file_path: str):
        self.file_path = file_path
        parent = os.path.dirname(self.file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _default_data(self) -> Dict[str, Any]:
        return {
            "active_profile": "",
            "profiles": {},
            "updated_at": _now_str(),
        }

    def _normalize(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return self._default_data()

        active_profile = str(data.get("active_profile") or "").strip()
        raw_profiles = data.get("profiles")
        profiles: Dict[str, Dict[str, str]] = {}

        if isinstance(raw_profiles, dict):
            for name, item in raw_profiles.items():
                clean_name = str(name or "").strip()
                if not clean_name:
                    continue
                if isinstance(item, dict):
                    description = str(item.get("description") or "")
                    created_at = str(item.get("created_at") or _now_str())
                    updated_at = str(item.get("updated_at") or _now_str())
                else:
                    description = str(item or "")
                    created_at = _now_str()
                    updated_at = _now_str()
                profiles[clean_name] = {
                    "description": description,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }

        if active_profile not in profiles:
            active_profile = ""

        return {
            "active_profile": active_profile,
            "profiles": profiles,
            "updated_at": str(data.get("updated_at") or _now_str()),
        }

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.file_path):
            return self._default_data()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return self._normalize(raw)
        except Exception:
            return self._default_data()

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize(data)
        normalized["updated_at"] = _now_str()
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
        return normalized

    def list_profiles(self) -> List[str]:
        data = self.load()
        names = list((data.get("profiles") or {}).keys())
        names.sort(key=lambda x: x.lower())
        return names

    def get_profile(self, name: str) -> Optional[Dict[str, str]]:
        clean_name = str(name or "").strip()
        if not clean_name:
            return None
        data = self.load()
        profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        item = profiles.get(clean_name)
        if not isinstance(item, dict):
            return None
        return {
            "name": clean_name,
            "description": str(item.get("description") or ""),
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }

    def save_profile(self, name: str, description: str) -> Dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("profile_name is required")

        data = self.load()
        profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        now = _now_str()
        prev = profiles.get(clean_name) if isinstance(profiles.get(clean_name), dict) else {}
        profiles[clean_name] = {
            "description": str(description or ""),
            "created_at": str(prev.get("created_at") or now),
            "updated_at": now,
        }
        data["profiles"] = profiles
        return self.save(data)

    def delete_profile(self, name: str) -> Dict[str, Any]:
        clean_name = str(name or "").strip()
        data = self.load()
        profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        if clean_name in profiles:
            profiles.pop(clean_name, None)
            if str(data.get("active_profile") or "").strip() == clean_name:
                data["active_profile"] = ""
        data["profiles"] = profiles
        return self.save(data)

    def set_active_profile(self, name: str) -> Dict[str, Any]:
        clean_name = str(name or "").strip()
        data = self.load()
        profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
        data["active_profile"] = clean_name if clean_name in profiles else ""
        return self.save(data)

    def get_state(self) -> Dict[str, Any]:
        data = self.load()
        names = list((data.get("profiles") or {}).keys())
        names.sort(key=lambda x: x.lower())
        return {
            "active_profile": str(data.get("active_profile") or ""),
            "available_profiles": names,
        }
