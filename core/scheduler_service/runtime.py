import asyncio
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.mcp_scheduler.schedule_utils import compute_next_run, is_due, normalize_schedule
from core.shared import RemoteMCPServer, StdioMCPToolClient

from .storage import SchedulerTaskStore, now_iso
from .template_utils import render_value


class SchedulerRuntime:
    def __init__(self, storage: SchedulerTaskStore, logger: Any):
        self.storage = storage
        self.logger = logger
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self.orchestrator_server = RemoteMCPServer(
            server_name="mcp_orchestrator",
            command=self._resolve_command(),
            args=self._resolve_args(),
            env=dict(os.environ),
            timeout_sec=max(1, int(str(os.getenv("MCP_TIMEOUT_SEC", "30")).strip() or "30")),
        )
        self.orchestrator_client = StdioMCPToolClient(logger=logger)

    def _resolve_command(self) -> str:
        configured = str(os.getenv("MCP_SERVER_COMMAND") or "").strip()
        if configured:
            return configured
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        venv_python = os.path.join(project_root, ".venv", "Scripts", "python.exe")
        if os.path.exists(venv_python):
            return venv_python
        return str(sys.executable).strip() or sys.executable
    def _resolve_args(self) -> List[str]:
        raw = str(os.getenv("MCP_SERVER_ARGS") or "").strip()
        if raw:
            import json
            import shlex
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed]
                except Exception:
                    pass
            try:
                return shlex.split(raw, posix=False)
            except Exception:
                return [raw]
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        return [os.path.join(project_root, "run_mcp_orchestrator.py")]

    async def start(self) -> None:
        self.logger.info("SCHEDULER_RUNTIME_START")
        await self.recalculate_all_next_runs()
        self._stop_event.clear()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop(), name="scheduler-runtime")

    async def stop(self) -> None:
        self.logger.info("SCHEDULER_RUNTIME_STOP")
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
                self.logger.exception("SCHEDULER_RUNTIME_LOOP_ERROR", {"message": str(e)})
            await asyncio.sleep(5)

    async def run_pending(self) -> None:
        async with self._run_lock:
            now = datetime.now()
            due_tasks: List[Dict[str, Any]] = []
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
        trace_id = str(uuid.uuid4())
        steps = task.get("steps") if isinstance(task.get("steps"), list) else []
        memory = task.get("execution_memory") if isinstance(task.get("execution_memory"), dict) else {}
        run_log: List[Dict[str, Any]] = []
        self.logger.info("SCHEDULER_TASK_RUN_START", {"task_id": task_id, "trace_id": trace_id, "steps": steps})
        try:
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    raise ValueError(f"Task step #{index} must be an object")
                tool_name = str(step.get("tool") or "").strip()
                if not tool_name:
                    raise ValueError(f"Task step #{index} missing tool")
                raw_args = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
                raw_template = step.get("arguments_template") if isinstance(step.get("arguments_template"), dict) else None
                arguments = dict(raw_args)
                if raw_template is not None:
                    rendered = render_value(raw_template, memory)
                    if not isinstance(rendered, dict):
                        raise ValueError(f"Task step #{index} arguments_template must render to an object")
                    arguments = rendered
                arguments["trace_id"] = trace_id
                self.logger.info(
                    "SCHEDULER_TASK_STEP_REQUEST",
                    {"task_id": task_id, "trace_id": trace_id, "step_index": index, "tool": tool_name, "arguments": arguments},
                )
                result = await self.orchestrator_client.call_tool(self.orchestrator_server, tool_name, arguments)
                self.logger.info(
                    "SCHEDULER_TASK_STEP_RESPONSE",
                    {"task_id": task_id, "trace_id": trace_id, "step_index": index, "tool": tool_name, "result": result},
                )
                run_log.append({"step_index": index, "tool": tool_name, "arguments": arguments, "result": result})
                if bool(result.get("is_error")) or bool(result.get("ok") is False and result.get("message")):
                    raise RuntimeError(str(result.get("message") or result.get("error") or f"Task step failed: {tool_name}"))
                save_key = str(step.get("save_result_as") or "").strip()
                if save_key:
                    memory[save_key] = result

            task["execution_memory"] = memory
            task["last_run_log"] = run_log
            task["last_error"] = ""
            task["last_run_at"] = now_iso()
            task["updated_at"] = now_iso()
            if str(task.get("schedule_type") or "").strip().lower() == "once":
                task["status"] = "completed"
                task["next_run_at"] = None
            else:
                task["next_run_at"] = self._recalculate_task_next_run(task)
            self.storage.save_task(task)
            self.logger.info("SCHEDULER_TASK_RUN_SUCCESS", {"task_id": task_id, "trace_id": trace_id, "memory": memory})
        except Exception as e:
            task["execution_memory"] = memory
            task["last_run_log"] = run_log
            task["last_error"] = str(e)
            task["updated_at"] = now_iso()
            task["last_failed_at"] = now_iso()
            self.storage.save_task(task)
            self.logger.error("SCHEDULER_TASK_RUN_ERROR", {"task_id": task_id, "trace_id": trace_id, "message": str(e), "run_log": run_log})

    async def create_task(
        self,
        *,
        title: str,
        schedule_type: str,
        schedule: Dict[str, Any],
        steps: List[Dict[str, Any]],
        template_text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        clean_schedule_type = str(schedule_type or "").strip().lower()
        if not clean_schedule_type:
            raise ValueError("schedule_type is required")
        if not isinstance(schedule, dict) or not schedule:
            raise ValueError("schedule is required")
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps is required and must be a non-empty list")
        normalized_schedule = normalize_schedule(clean_schedule_type, schedule)
        task_id = str(uuid.uuid4())
        now_value = now_iso()
        task = {
            "task_id": task_id,
            "title": str(title or "").strip() or f"task_{task_id[:8]}",
            "status": "active",
            "created_at": now_value,
            "updated_at": now_value,
            "schedule_type": clean_schedule_type,
            "schedule": normalized_schedule,
            "steps": steps,
            "template_text": str(template_text or ""),
            "metadata": dict(metadata or {}),
            "execution_memory": {},
            "last_run_log": [],
            "last_run_at": "",
            "last_failed_at": "",
            "last_error": "",
            "timezone": self.storage.get_timezone(),
            "next_run_at": None,
        }
        task["next_run_at"] = self._recalculate_task_next_run(task)
        self.storage.save_task(task)
        return task

    async def list_tasks(self, *, status: str = "") -> List[Dict[str, Any]]:
        status_filter = str(status or "").strip().lower()
        tasks = self.storage.list_tasks(include_inactive=True)
        if status_filter:
            tasks = [task for task in tasks if str(task.get("status") or "").strip().lower() == status_filter]
        tasks.sort(key=lambda item: str(item.get("next_run_at") or "9999"))
        return tasks

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        task = self.storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found")
        return task

    async def delete_task(self, task_id: str) -> Dict[str, Any]:
        deleted = self.storage.delete_task(task_id)
        if not deleted:
            raise ValueError(f"Task '{task_id}' not found")
        return {"task_id": task_id, "status": "deleted"}


