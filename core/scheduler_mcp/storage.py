import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SchedulerStorage:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)
        self.tasks_path = os.path.join(self.base_dir, "tasks.json")
        self.bindings_path = os.path.join(self.base_dir, "telegram_bindings.json")
        self.settings_path = os.path.join(self.base_dir, "settings.json")
        self._ensure_file(self.tasks_path, {"tasks": []})
        self._ensure_file(self.bindings_path, {"bindings": []})
        self._ensure_file(self.settings_path, {"timezone": "Europe/Moscow"})

    def _ensure_file(self, path: str, default_payload: Dict[str, Any]) -> None:
        if os.path.exists(path):
            return
        self._write_json(path, default_payload)

    def _read_json(self, path: str, default_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return deepcopy(default_payload)

    def _write_json(self, path: str, payload: Dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix="scheduler_", suffix=".json", dir=self.base_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def list_tasks(self, *, include_inactive: bool = True) -> List[Dict[str, Any]]:
        payload = self._read_json(self.tasks_path, {"tasks": []})
        items = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        tasks = [item for item in items if isinstance(item, dict)]
        if include_inactive:
            return tasks
        return [task for task in tasks if str(task.get("status") or "").lower() == "active"]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return None
        for task in self.list_tasks(include_inactive=True):
            if str(task.get("task_id") or "") == clean_id:
                return task
        return None

    def save_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        tasks = self.list_tasks(include_inactive=True)
        clean_id = str(task.get("task_id") or "").strip()
        if not clean_id:
            raise ValueError("task_id is required")
        updated = False
        for index, item in enumerate(tasks):
            if str(item.get("task_id") or "") == clean_id:
                tasks[index] = task
                updated = True
                break
        if not updated:
            tasks.append(task)
        self._write_json(self.tasks_path, {"tasks": tasks})
        return task

    def delete_task(self, task_id: str) -> bool:
        tasks = self.list_tasks(include_inactive=True)
        before = len(tasks)
        tasks = [task for task in tasks if str(task.get("task_id") or "") != str(task_id or "")]
        if len(tasks) == before:
            return False
        self._write_json(self.tasks_path, {"tasks": tasks})
        return True

    def list_bindings(self) -> List[Dict[str, Any]]:
        payload = self._read_json(self.bindings_path, {"bindings": []})
        items = payload.get("bindings") if isinstance(payload.get("bindings"), list) else []
        return [item for item in items if isinstance(item, dict)]

    def get_binding(self, telegram_username: str) -> Optional[Dict[str, Any]]:
        normalized = str(telegram_username or "").strip().lstrip("@").lower()
        if not normalized:
            return None
        for binding in self.list_bindings():
            if str(binding.get("telegram_username") or "").strip().lstrip("@").lower() == normalized:
                return binding
        return None

    def save_binding(self, binding: Dict[str, Any]) -> Dict[str, Any]:
        bindings = self.list_bindings()
        normalized = str(binding.get("telegram_username") or "").strip().lstrip("@").lower()
        if not normalized:
            raise ValueError("telegram_username is required")
        updated = False
        for index, item in enumerate(bindings):
            if str(item.get("telegram_username") or "").strip().lstrip("@").lower() == normalized:
                bindings[index] = binding
                updated = True
                break
        if not updated:
            bindings.append(binding)
        self._write_json(self.bindings_path, {"bindings": bindings})
        return binding

    def get_timezone(self) -> str:
        payload = self._read_json(self.settings_path, {"timezone": "Europe/Moscow"})
        return str(payload.get("timezone") or "Europe/Moscow")
