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
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _safe_id(self, session_id: str) -> str:
        return "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_"))

    def _session_file_path_today(self, session_id: str) -> str:
        day = datetime.now().strftime("%Y%m%d")
        safe_id = self._safe_id(session_id)
        # ВАЖНО: memmory — как ты написал
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
                    return data
            except Exception:
                pass

        created_at = _now_iso()
        data = {
            "session_id": session_id,
            "title": "",
            "created_at": created_at,
            "updated_at": created_at,
            "messages": [],
            "file_path": self._session_file_path_today(session_id),
        }
        return data

    def save_session(self, session: Dict[str, Any]) -> str:
        session_id = (session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        path = session.get("file_path") or self._session_file_path_today(session_id)
        session["file_path"] = path

        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

        return path

    def set_title_if_empty(self, session: Dict[str, Any], user_text: str) -> None:
        title = (session.get("title") or "").strip()
        if title:
            return

        t = " ".join((user_text or "").strip().split())
        if len(t) > 60:
            t = t[:60].rstrip() + "…"
        session["title"] = t or "Без темы"