import os, sys
sys.dont_write_bytecode = True

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SessionInfo:
    session_id: str
    title: str
    created_at: str
    updated_at: str
    file_path: str


class AgentMemoryStore:
    """
    Хранит сессии на диске (json) в стиле твоего проекта (memmoryYYYYMMDD).
    Внутри сессии теперь поддерживаются ветки (branches) и facts.

    Формат (упрощённый):
    {
      "session_id": "...",
      "title": "...",
      "created_at": "...",
      "updated_at": "...",
      "active_branch": "main",
      "branches": {
         "main": {
            "branch_id": "main",
            "name": "main",
            "created_at": "...",
            "updated_at": "...",
            "facts": {"key":"value"},
            "checkpoints": {"cp1": 6},
            "history": [{"role":"user","content":"..."}, ...]
         },
         "b2": {...}
      }
    }
    """
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _safe_id(self, session_id: str) -> str:
        return "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_"))

    def _session_file_path_today(self, session_id: str) -> str:
        day = datetime.now().strftime("%Y%m%d")
        safe_id = self._safe_id(session_id)
        return os.path.join(self.base_dir, f"{safe_id}_memmory{day}.json")

    def _find_latest_file_for_session(self, session_id: str) -> Optional[str]:
        safe_id = self._safe_id(session_id)
        candidates: List[str] = []
        try:
            for name in os.listdir(self.base_dir):
                if name.startswith(f"{safe_id}_memmory") and name.endswith(".json"):
                    candidates.append(os.path.join(self.base_dir, name))
        except Exception:
            return None

        if not candidates:
            return None

        try:
            candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except Exception:
            pass

        return candidates[0]

    def list_sessions(self) -> List[SessionInfo]:
        sessions: Dict[str, SessionInfo] = {}

        try:
            for name in os.listdir(self.base_dir):
                if not (name.endswith(".json") and "_memmory" in name):
                    continue

                path = os.path.join(self.base_dir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue

                session_id = (data.get("session_id") or "").strip()
                if not session_id:
                    continue

                title = (data.get("title") or "").strip()
                created_at = data.get("created_at") or ""
                updated_at = data.get("updated_at") or ""

                info = SessionInfo(
                    session_id=session_id,
                    title=title,
                    created_at=created_at,
                    updated_at=updated_at,
                    file_path=path,
                )

                if session_id not in sessions:
                    sessions[session_id] = info
                else:
                    try:
                        if os.path.getmtime(path) > os.path.getmtime(sessions[session_id].file_path):
                            sessions[session_id] = info
                    except Exception:
                        sessions[session_id] = info
        except Exception:
            return []

        result = list(sessions.values())
        try:
            result.sort(key=lambda s: s.updated_at or "", reverse=True)
        except Exception:
            pass
        return result

    def load_session(self, session_id: str) -> Dict[str, Any]:
        path = self._find_latest_file_for_session(session_id)

        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict) and data.get("session_id") == session_id:
                    data["file_path"] = path
                    return self._migrate(data)
            except Exception:
                pass

        created_at = _now_iso()
        session = {
            "session_id": session_id,
            "title": "",
            "created_at": created_at,
            "updated_at": created_at,
            "active_branch": "main",
            "branches": {
                "main": {
                    "branch_id": "main",
                    "name": "main",
                    "created_at": created_at,
                    "updated_at": created_at,
                    "facts": {},
                    "checkpoints": {},
                    "history": [],
                }
            },
            "file_path": self._session_file_path_today(session_id),
        }
        return session

    def _migrate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # На всякий — если остался старый формат history(dict turns) — конвертируем в messages list
        if "branches" not in data:
            created_at = data.get("created_at") or _now_iso()
            updated_at = data.get("updated_at") or created_at

            # поддержка старого формата: history как dict turn_id -> {...}
            msgs: List[Dict[str, str]] = []
            if isinstance(data.get("history"), dict):
                try:
                    keys = sorted(list(data["history"].keys()), key=lambda x: int(x))
                except Exception:
                    keys = list(data["history"].keys())

                for k in keys:
                    t = data["history"].get(k) or {}
                    ut = t.get("user_text") or ""
                    at = t.get("assistant_text") or ""
                    if ut:
                        msgs.append({"role": "user", "content": str(ut)})
                    if at:
                        msgs.append({"role": "assistant", "content": str(at)})

            # старый формат messages(list role/content)
            if not msgs and isinstance(data.get("messages"), list):
                for m in data["messages"]:
                    role = (m.get("role") or "").strip()
                    content = m.get("content")
                    if role and isinstance(content, str):
                        msgs.append({"role": role, "content": content})

            data = {
                "session_id": data.get("session_id"),
                "title": data.get("title") or "",
                "created_at": created_at,
                "updated_at": updated_at,
                "active_branch": "main",
                "branches": {
                    "main": {
                        "branch_id": "main",
                        "name": "main",
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "facts": {},
                        "checkpoints": {},
                        "history": msgs,
                    }
                },
                "file_path": data.get("file_path"),
            }

        if not isinstance(data.get("active_branch"), str) or not data.get("active_branch"):
            data["active_branch"] = "main"

        if not isinstance(data.get("branches"), dict) or not data["branches"]:
            now = _now_iso()
            data["branches"] = {
                "main": {
                    "branch_id": "main",
                    "name": "main",
                    "created_at": now,
                    "updated_at": now,
                    "facts": {},
                    "checkpoints": {},
                    "history": [],
                }
            }
            data["active_branch"] = "main"

        # normalize each branch
        for bid, b in list(data["branches"].items()):
            if not isinstance(b, dict):
                data["branches"].pop(bid, None)
                continue
            b.setdefault("branch_id", bid)
            b.setdefault("name", bid)
            b.setdefault("created_at", data.get("created_at") or _now_iso())
            b.setdefault("updated_at", data.get("updated_at") or _now_iso())
            if not isinstance(b.get("facts"), dict):
                b["facts"] = {}
            if not isinstance(b.get("checkpoints"), dict):
                b["checkpoints"] = {}
            if not isinstance(b.get("history"), list):
                b["history"] = []

        # ensure active exists
        if data["active_branch"] not in data["branches"]:
            data["active_branch"] = "main" if "main" in data["branches"] else next(iter(data["branches"].keys()))

        return data

    def save_session(self, session: Dict[str, Any]) -> str:
        session_id = (session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        path = session.get("file_path") or self._session_file_path_today(session_id)
        session["file_path"] = path

        session = self._migrate(session)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

        return path

    def delete_session_file(self, session_id: str) -> bool:
        path = self._find_latest_file_for_session(session_id)
        if not path:
            return False
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except Exception:
            return False

    def set_title_if_empty(self, session: Dict[str, Any], user_text: str) -> None:
        title = (session.get("title") or "").strip()
        if title:
            return

        t = " ".join((user_text or "").strip().split())
        if len(t) > 60:
            t = t[:60].rstrip() + "…"
        session["title"] = t or "Без темы"
