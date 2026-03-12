import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.scheduler_mcp.jobs import execute_job
from core.scheduler_mcp.schedule_utils import compute_next_run, is_due, normalize_schedule, parse_iso_datetime
from core.scheduler_mcp.storage import SchedulerStorage, now_iso
from core.scheduler_mcp.telegram_client import TelegramClient


class SchedulerRuntime:
    def __init__(self, storage: SchedulerStorage, telegram_client: TelegramClient, logger: logging.Logger):
        self.storage = storage
        self.telegram = telegram_client
        self.logger = logger
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        self.logger.info("Scheduler runtime starting")
        await self.recalculate_all_next_runs()
        self._stop_event.clear()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop(), name="scheduler-mcp-runtime")

    async def stop(self) -> None:
        self.logger.info("Scheduler runtime stopping")
        self._stop_event.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def recalculate_all_next_runs(self) -> None:
        now = datetime.now()
        for task in self.storage.list_tasks(include_inactive=True):
            if str(task.get("status") or "").lower() != "active":
                continue
            task["next_run_at"] = self._recalculate_task_next_run(task, now=now)
            task["updated_at"] = now_iso()
            self.storage.save_task(task)

    def _recalculate_task_next_run(self, task: Dict[str, Any], *, now: Optional[datetime] = None) -> Optional[str]:
        schedule = task.get("schedule") if isinstance(task.get("schedule"), dict) else {}
        schedule_type = str(task.get("schedule_type") or "").strip().lower()
        next_run_at = compute_next_run(schedule_type, schedule, now=now, last_run=task.get("last_run_at"))
        if schedule_type == "once" and task.get("last_run_at"):
            return None
        return next_run_at

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_pending()
            except Exception as e:
                self.logger.exception("Scheduler runtime loop failure: %s", e)
            await asyncio.sleep(5)

    async def run_pending(self) -> None:
        async with self._run_lock:
            now = datetime.now()
            due_tasks = []
            for task in self.storage.list_tasks(include_inactive=False):
                next_run_at = str(task.get("next_run_at") or "").strip()
                if not next_run_at:
                    next_run_at = self._recalculate_task_next_run(task, now=now)
                    task["next_run_at"] = next_run_at
                    task["updated_at"] = now_iso()
                    self.storage.save_task(task)
                if is_due(next_run_at, now=now):
                    due_tasks.append(task)
            for task in due_tasks:
                await self._execute_task(task)

    async def _execute_task(self, task: Dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        try:
            binding = task.get("recipient") if isinstance(task.get("recipient"), dict) else {}
            chat_id = str(binding.get("chat_id") or "").strip()
            if not chat_id:
                raise ValueError("task recipient.chat_id is missing")
            message = execute_job(task)
            await self.telegram.send_message(chat_id=chat_id, text=message)
            task["last_run_at"] = now_iso()
            task["last_error"] = ""
            task["updated_at"] = now_iso()
            if str(task.get("schedule_type") or "").strip().lower() == "once":
                task["status"] = "completed"
                task["next_run_at"] = None
            else:
                task["next_run_at"] = self._recalculate_task_next_run(task)
            self.storage.save_task(task)
            self.logger.info("Task executed successfully: %s", task_id)
        except Exception as e:
            task["last_error"] = str(e)
            task["updated_at"] = now_iso()
            self.storage.save_task(task)
            self.logger.exception("Task execution failed: %s", task_id)

    async def bind_recipient(self, telegram_username: str) -> Dict[str, Any]:
        normalized = str(telegram_username or "").strip().lstrip("@").lower()
        if not normalized:
            raise ValueError("telegram_username is required")

        existing = self.storage.get_binding(normalized)
        if existing is not None:
            existing["last_verified_at"] = now_iso()
            self.storage.save_binding(existing)
            self.logger.info("Telegram binding reused for @%s", normalized)
            return {"status": "already_bound", "binding": existing, "message": f"Telegram recipient @{normalized} already bound."}

        updates = await self.telegram.get_updates(limit=100)
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
            self.logger.info("Telegram binding created for @%s chat_id=%s", normalized, chat_id)
            return {"status": "bound", "binding": binding, "message": f"Telegram recipient @{normalized} was bound successfully."}

        self.logger.warning("Telegram binding not found for @%s", normalized)
        return {
            "status": "not_found",
            "binding": None,
            "message": (
                f"Telegram recipient @{normalized} not found in bot updates. "
                "Ask the user to send any message to the bot first, then call this tool again."
            ),
        }

    async def create_task(
        self,
        *,
        title: str,
        job_type: str,
        job_payload: Dict[str, Any],
        schedule_type: str,
        schedule: Dict[str, Any],
        telegram_username: str,
    ) -> Dict[str, Any]:
        clean_username = str(telegram_username or "").strip().lstrip("@").lower()
        clean_job_type = str(job_type or "").strip().lower()
        clean_schedule_type = str(schedule_type or "").strip().lower()
        if not clean_schedule_type:
            raise ValueError("schedule_type is required")
        if not isinstance(schedule, dict) or not schedule:
            if clean_schedule_type == "interval":
                raise ValueError(
                    "schedule is required. For interval tasks send schedule={'every':10,'unit':'minutes'} or schedule={'every':2,'unit':'hours'}."
                )
            if clean_schedule_type == "once":
                raise ValueError("schedule is required. For one-time tasks send schedule={'run_at':'YYYY-MM-DD HH:MM'}.")
            if clean_schedule_type == "daily":
                raise ValueError("schedule is required. For daily tasks send schedule={'time_points':[{'hour':9,'minute':0}]}.")
            if clean_schedule_type == "weekly":
                raise ValueError(
                    "schedule is required. For weekly tasks send schedule={'days':['monday'],'time_points':[{'hour':9,'minute':0}]}."
                )
            raise ValueError("schedule is required")
        if clean_job_type == "weather_summary" and (not isinstance(job_payload, dict) or not str(job_payload.get('city') or '').strip()):
            raise ValueError("job_payload.city is required for weather_summary")

        binding = self.storage.get_binding(clean_username)
        if binding is None:
            raise ValueError(
                f"Telegram recipient @{clean_username} is not bound. Call telegram_bind_recipient first after the user sends a message to the bot."
            )

        normalized_schedule = normalize_schedule(schedule_type, schedule)
        task_id = str(uuid.uuid4())
        now_value = now_iso()
        task = {
            "task_id": task_id,
            "title": str(title or "").strip() or f"{job_type}_{task_id[:8]}",
            "status": "active",
            "created_at": now_value,
            "updated_at": now_value,
            "schedule_type": clean_schedule_type,
            "schedule": normalized_schedule,
            "recipient": binding,
            "job_type": clean_job_type,
            "job_payload": dict(job_payload or {}),
            "next_run_at": None,
            "last_run_at": "",
            "last_error": "",
            "timezone": self.storage.get_timezone(),
        }
        task["next_run_at"] = self._recalculate_task_next_run(task)
        if task["next_run_at"] is None and task["schedule_type"] != "once":
            raise ValueError("Could not compute next_run_at for the provided schedule")
        self.storage.save_task(task)
        self.logger.info("Task created: %s title=%s", task_id, task["title"])
        return task

    async def list_tasks(self, *, status: str = "", telegram_username: str = "") -> List[Dict[str, Any]]:
        items = self.storage.list_tasks(include_inactive=True)
        status_filter = str(status or "").strip().lower()
        username_filter = str(telegram_username or "").strip().lstrip("@").lower()
        filtered: List[Dict[str, Any]] = []
        for task in items:
            if status_filter and str(task.get("status") or "").strip().lower() != status_filter:
                continue
            recipient = task.get("recipient") if isinstance(task.get("recipient"), dict) else {}
            if username_filter and str(recipient.get("telegram_username") or "").strip().lstrip("@").lower() != username_filter:
                continue
            filtered.append(task)
        filtered.sort(key=lambda item: str(item.get("next_run_at") or "9999"))
        return filtered

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        task = self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found")
        deleted = self.storage.delete_task(task_id)
        if not deleted:
            raise ValueError(f"Task '{task_id}' not found")
        self.logger.info("Task cancelled: %s", task_id)
        return {"task_id": task_id, "status": "cancelled"}
