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

    # === Инициализация и файловые пути ===

    # Готовит каталог хранилища, нормализует session_id и вычисляет актуальный путь файла сессии по дате.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _safe_id(self, session_id: str) -> str:
        return "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_"))

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _session_file_path_today(self, session_id: str) -> str:
        day = datetime.now().strftime("%Y%m%d")
        safe_id = self._safe_id(session_id)
        return os.path.join(self.base_dir, f"{safe_id}_memmory{day}.json")

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

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

    # === Чтение и миграция сессий ===

    # Сканирует доступные файлы сессий, загружает нужную запись и приводит старые форматы истории к текущей веточной структуре.

    # Возвращает агрегированный список сущностей в упорядоченном виде для отображения в UI или дальнейшей логики.

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

    # Читает последнюю версию сессии с диска, выполняет миграцию старого формата и гарантирует целостную branching-структуру перед дальнейшей работой.

    def load_session(self, session_id: str) -> Dict[str, Any]:
        path = self._find_latest_file_for_session(session_id)

        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict) and data.get("session_id") == session_id:
                    data["file_path"] = path

                    # --- миграция старого формата messages -> history
                    if "history" not in data:
                        old_messages = data.get("messages")
                        if isinstance(old_messages, list):
                            history: Dict[str, Any] = {}
                            idx = 0
                            pending_user = None

                            for m in old_messages:
                                role = (m.get("role") or "").strip()
                                content = m.get("content")

                                if role == "user" and isinstance(content, str):
                                    pending_user = {"text": content, "ts": m.get("ts")}
                                elif role == "assistant" and isinstance(content, str):
                                    if pending_user is None:
                                        pending_user = {"text": "", "ts": m.get("ts")}
                                    idx += 1
                                    history[str(idx)] = {
                                        "ts": pending_user.get("ts"),
                                        "user_text": pending_user.get("text") or "",
                                        "assistant_text": content,
                                        "model": None,
                                        "endpoint": None,
                                        "usage": {},
                                        "cost_rub": None,
                                        "r_prompt_total": 0,
                                        "c_completion": 0,
                                        "total_tokens_call": 0,
                                        "r_prev_prompt_total": 0,
                                        "current_message_tokens": 0,
                                    }
                                    pending_user = None

                            if pending_user is not None:
                                idx += 1
                                history[str(idx)] = {
                                    "ts": pending_user.get("ts"),
                                    "user_text": pending_user.get("text") or "",
                                    "assistant_text": "",
                                    "model": None,
                                    "endpoint": None,
                                    "usage": {},
                                    "cost_rub": None,
                                    "r_prompt_total": 0,
                                    "c_completion": 0,
                                    "total_tokens_call": 0,
                                    "r_prev_prompt_total": 0,
                                    "current_message_tokens": 0,
                                }

                            data["history"] = history
                            data.pop("messages", None)

                    if not isinstance(data.get("history"), dict):
                        data["history"] = {}

                    # --- гарантируем наличие history_summary (может остаться пустым, в Day10 summary не используем)
                    if "history_summary" not in data or not isinstance(data.get("history_summary"), str):
                        data["history_summary"] = ""

                    # --- NEW: Branching структура
                    # branches: { "main": {title, history(list), facts(dict), checkpoints(list), created_at, updated_at}, ... }
                    if not isinstance(data.get("branches"), dict):
                        data["branches"] = {}

                    branches: Dict[str, Any] = data["branches"]

                    # Миграция: если branches пустые, а history(dict) есть — переложим в main как список сообщений
                    if "main" not in branches:
                        branches["main"] = {
                            "title": "main",
                            "history": [],          # список сообщений role/content
                            "facts": {},
                            "checkpoints": [],
                            "summary": "",
                            "memory_layers": {
                                "short_term": [],
                                "working": {},
                                "long_term": {},
                            },
                            "created_at": data.get("created_at") or _now_iso(),
                            "updated_at": data.get("updated_at") or _now_iso(),
                        }

                        # Перенос turn-based history -> messages list
                        # history: {"1": {user_text, assistant_text}, ...}
                        h = data.get("history") if isinstance(data.get("history"), dict) else {}
                        keys = []
                        try:
                            keys = sorted([int(k) for k in h.keys() if str(k).isdigit()])
                        except Exception:
                            keys = []

                        out = []
                        for k in keys:
                            t = h.get(str(k)) or {}
                            ut = t.get("user_text")
                            at = t.get("assistant_text")
                            if isinstance(ut, str) and ut.strip():
                                out.append({"role": "user", "content": ut})
                            if isinstance(at, str) and at.strip():
                                out.append({"role": "assistant", "content": at})

                        branches["main"]["history"] = out

                    # active_branch
                    if not isinstance(data.get("active_branch"), str) or not data.get("active_branch"):
                        data["active_branch"] = "main"
                    if data["active_branch"] not in branches:
                        data["active_branch"] = "main"

                    # нормализация веток
                    for bid, b in list(branches.items()):
                        if not isinstance(b, dict):
                            branches.pop(bid, None)
                            continue
                        if not isinstance(b.get("title"), str):
                            b["title"] = bid
                        if not isinstance(b.get("history"), list):
                            b["history"] = []
                        if not isinstance(b.get("facts"), dict):
                            b["facts"] = {}
                        if not isinstance(b.get("checkpoints"), list):
                            b["checkpoints"] = []
                        if not isinstance(b.get("summary"), str):
                            b["summary"] = ""
                        layers = b.get("memory_layers")
                        if not isinstance(layers, dict):
                            layers = {}
                        short_term = layers.get("short_term")
                        working = layers.get("working")
                        long_term = layers.get("long_term")
                        b["memory_layers"] = {
                            "short_term": short_term if isinstance(short_term, list) else [],
                            "working": working if isinstance(working, dict) else {},
                            "long_term": long_term if isinstance(long_term, dict) else {},
                        }
                        if not isinstance(b.get("created_at"), str):
                            b["created_at"] = data.get("created_at") or _now_iso()
                        if not isinstance(b.get("updated_at"), str):
                            b["updated_at"] = data.get("updated_at") or _now_iso()

                    data["branches"] = branches
                    return data

            except Exception:
                pass

        # --- если сессии нет, создаём новую
        now = _now_iso()
        data = {
            "session_id": session_id,
            "title": "",
            "created_at": now,
            "updated_at": now,
            "history": {},            # старое поле оставляем пустым
            "history_summary": "",
            "active_branch": "main",
            "branches": {
                "main": {
                    "title": "main",
                    "history": [],
                    "facts": {},
                    "checkpoints": [],
                    "summary": "",
                    "memory_layers": {
                        "short_term": [],
                        "working": {},
                        "long_term": {},
                    },
                    "created_at": now,
                    "updated_at": now,
                }
            },
            "file_path": self._session_file_path_today(session_id),
        }
        return data

    # === Сохранение и сервисные операции ===

    # Перед записью нормализует структуру веток/памяти, удаляет файл сессии по id и генерирует заголовок из первого user-запроса.

    # Проводит финальную нормализацию веток/слоёв памяти и атомарно сохраняет сессию в JSON, сохраняя совместимость со старыми полями.

    def save_session(self, session: Dict[str, Any]) -> str:
        session_id = (session.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        path = session.get("file_path") or self._session_file_path_today(session_id)
        session["file_path"] = path

        # --- гарантируем branching структуру
        if not isinstance(session.get("branches"), dict):
            session["branches"] = {}

        if not isinstance(session.get("active_branch"), str) or not session.get("active_branch"):
            session["active_branch"] = "main"

        branches = session["branches"]
        if "main" not in branches or not isinstance(branches.get("main"), dict):
            branches["main"] = {
                "title": "main",
                "history": [],
                "facts": {},
                "checkpoints": [],
                "summary": "",
                "memory_layers": {
                    "short_term": [],
                    "working": {},
                    "long_term": {},
                },
                "created_at": session.get("created_at") or _now_iso(),
                "updated_at": session.get("updated_at") or _now_iso(),
            }

        # Нормализация веток
        for bid, b in list(branches.items()):
            if not isinstance(b, dict):
                branches.pop(bid, None)
                continue
            if not isinstance(b.get("title"), str):
                b["title"] = bid
            if not isinstance(b.get("history"), list):
                b["history"] = []
            if not isinstance(b.get("facts"), dict):
                b["facts"] = {}
            if not isinstance(b.get("checkpoints"), list):
                b["checkpoints"] = []
            if not isinstance(b.get("summary"), str):
                b["summary"] = ""
            layers = b.get("memory_layers")
            if not isinstance(layers, dict):
                layers = {}
            short_term = layers.get("short_term")
            working = layers.get("working")
            long_term = layers.get("long_term")
            b["memory_layers"] = {
                "short_term": short_term if isinstance(short_term, list) else [],
                "working": working if isinstance(working, dict) else {},
                "long_term": long_term if isinstance(long_term, dict) else {},
            }
            if not isinstance(b.get("created_at"), str):
                b["created_at"] = session.get("created_at") or _now_iso()
            if not isinstance(b.get("updated_at"), str):
                b["updated_at"] = session.get("updated_at") or _now_iso()

        if session["active_branch"] not in branches:
            session["active_branch"] = "main"

        session["branches"] = branches

        # --- старые поля оставляем, но не обязаны их наполнять
        if "history" not in session or not isinstance(session.get("history"), dict):
            session["history"] = {}

        if "history_summary" not in session or not isinstance(session.get("history_summary"), str):
            session["history_summary"] = ""

        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)

        return path

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

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

    # Обновляет внутреннее состояние объекта и синхронизирует связанные элементы интерфейса или данные.

    def set_title_if_empty(self, session: Dict[str, Any], user_text: str) -> None:
        title = (session.get("title") or "").strip()
        if title:
            return

        t = " ".join((user_text or "").strip().split())
        if len(t) > 60:
            t = t[:60].rstrip() + "…"
        session["title"] = t or "Без темы"
