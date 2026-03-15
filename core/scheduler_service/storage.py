import os
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.shared import JsonFileStore


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SchedulerTaskStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.store = JsonFileStore(base_dir)
        self.tasks_path = os.path.join(self.base_dir, "tasks.json")
        self.settings_path = os.path.join(self.base_dir, "settings.json")
        self.store.ensure_file(self.tasks_path, {"tasks": []})
        self.store.ensure_file(self.settings_path, {"timezone": "Europe/Moscow"})

    def list_tasks(self, *, include_inactive: bool = True) -> List[Dict[str, Any]]:
        payload = self.store.read_json(self.tasks_path, {"tasks": []})
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        clean = [deepcopy(item) for item in tasks if isinstance(item, dict)]
        if include_inactive:
            return clean
        return [item for item in clean if str(item.get("status") or "").lower() == "active"]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return None
        for task in self.list_tasks(include_inactive=True):
            if str(task.get("task_id") or "") == clean_id:
                return task
        return None

    def save_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        tasks = self.list_tasks(include_inactive=True)
        updated = False
        for index, item in enumerate(tasks):
            if str(item.get("task_id") or "") == task_id:
                tasks[index] = deepcopy(task)
                updated = True
                break
        if not updated:
            tasks.append(deepcopy(task))
        self.store.write_json(self.tasks_path, {"tasks": tasks})
        return deepcopy(task)

    def delete_task(self, task_id: str) -> bool:
        tasks = self.list_tasks(include_inactive=True)
        before = len(tasks)
        tasks = [task for task in tasks if str(task.get("task_id") or "") != str(task_id or "")]
        if len(tasks) == before:
            return False
        self.store.write_json(self.tasks_path, {"tasks": tasks})
        return True

    def get_timezone(self) -> str:
        payload = self.store.read_json(self.settings_path, {"timezone": "Europe/Moscow"})
        return str(payload.get("timezone") or "Europe/Moscow")
