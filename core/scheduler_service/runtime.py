import asyncio
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.mcp_scheduler.schedule_utils import compute_next_run, is_due, validate_schedule_definition
from core.shared import RemoteMCPServer, StdioMCPToolClient
from core.shared.schema_validation import validate_json_value

from .contracts import validate_scheduler_create_task_payload
from .storage import SchedulerTaskStore, now_iso
from .template_utils import render_value


class SchedulerRuntime:
    def __init__(self, storage: SchedulerTaskStore, logger: Any):
        self.storage = storage
        self.logger = logger
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._tool_schema_cache: Dict[str, Dict[str, Any]] = {}
        self._tool_schema_cache_at: float = 0.0
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

    async def _get_orchestrator_tool_schemas(self, *, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        if not force_refresh and self._tool_schema_cache and (time.monotonic() - self._tool_schema_cache_at) < 60:
            return dict(self._tool_schema_cache)

        tools = await self.orchestrator_client.list_tools(self.orchestrator_server)
        schemas: Dict[str, Dict[str, Any]] = {}
        for item in tools or []:
            name = str(getattr(item, "name", "") or (item.get("name") if isinstance(item, dict) else "")).strip()
            if not name:
                continue
            schema = getattr(item, "inputSchema", None)
            if schema is None and isinstance(item, dict):
                schema = item.get("inputSchema") or item.get("input_schema")
            if schema is None:
                schema = getattr(item, "input_schema", None)
            schemas[name] = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        self._tool_schema_cache = dict(schemas)
        self._tool_schema_cache_at = time.monotonic()
        return schemas

    def _schema_required_keys(self, schema: Dict[str, Any]) -> List[str]:
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        return [str(item) for item in required if str(item) and str(item) != "trace_id"]

    def _validate_step_payload_against_schema(
        self,
        *,
        step: Dict[str, Any],
        step_index: int,
        tool_schemas: Dict[str, Dict[str, Any]],
        rendered_arguments: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        errors: List[str] = []
        tool_name = str(step.get("tool") or "").strip()
        step_path = f"steps[{step_index}]"
        schema = tool_schemas.get(tool_name)
        if schema is None:
            return [f"{step_path}.tool: unknown orchestrator tool '{tool_name}'"]

        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = self._schema_required_keys(schema)
        payload = rendered_arguments if rendered_arguments is not None else (step.get("arguments") if isinstance(step.get("arguments"), dict) else {})
        if rendered_arguments is not None:
            effective_payload = dict(payload or {})
            effective_payload["trace_id"] = "runtime-trace"
            schema_errors = validate_json_value(effective_payload, schema, path=f"{step_path}.arguments")
            return [error.replace("runtime-trace", "<trace_id>") for error in schema_errors if "unexpected field 'trace_id'" not in error]

        if not isinstance(payload, dict):
            payload = {}
        template_payload = step.get("arguments_template") if isinstance(step.get("arguments_template"), dict) else {}
        provided_keys = set(payload.keys()) | set(template_payload.keys())
        missing = [key for key in required if key not in provided_keys]
        if missing:
            errors.append(f"{step_path}: missing required tool arguments {', '.join(missing)}")

        if schema.get("additionalProperties") is False:
            unknown = [key for key in provided_keys if key not in properties]
            if unknown:
                errors.append(f"{step_path}: unexpected tool argument keys {', '.join(sorted(unknown))}")

        if payload:
            schema_errors = validate_json_value({**payload, "trace_id": "runtime-trace"}, schema, path=f"{step_path}.arguments")
            for error in schema_errors:
                if "missing required field" in error and any(key in error for key in required):
                    continue
                if "unexpected field 'trace_id'" in error:
                    continue
                errors.append(error.replace("runtime-trace", "<trace_id>"))
        return errors

    async def _validate_task_route(self, steps: List[Dict[str, Any]]) -> None:
        tool_schemas = await self._get_orchestrator_tool_schemas(force_refresh=True)
        errors: List[str] = []
        for index, step in enumerate(steps, start=1):
            errors.extend(self._validate_step_payload_against_schema(step=step, step_index=index, tool_schemas=tool_schemas))
        if errors:
            raise ValueError("Invalid task route: " + " | ".join(errors))

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
            if due_tasks:
                self.logger.info(
                    "SCHEDULER_RUNTIME_DUE_TASKS",
                    {
                        "count": len(due_tasks),
                        "task_ids": [str(task.get("task_id") or "") for task in due_tasks],
                        "checked_at": now_iso(),
                    },
                )
            for task in due_tasks:
                await self._execute_task(task)

    async def _execute_task(self, task: Dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        trace_id = str(uuid.uuid4())
        steps = task.get("steps") if isinstance(task.get("steps"), list) else []
        memory = task.get("execution_memory") if isinstance(task.get("execution_memory"), dict) else {}
        run_log: List[Dict[str, Any]] = []
        tool_schemas = await self._get_orchestrator_tool_schemas(force_refresh=True)
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
                if raw_template is not None and raw_args:
                    raise ValueError(f"Task step #{index} must not define both arguments and arguments_template")
                arguments = dict(raw_args)
                if raw_template is not None:
                    rendered = render_value(raw_template, memory)
                    if not isinstance(rendered, dict):
                        raise ValueError(f"Task step #{index} arguments_template must render to an object")
                    arguments = rendered
                rendered_errors = self._validate_step_payload_against_schema(
                    step=step,
                    step_index=index,
                    tool_schemas=tool_schemas,
                    rendered_arguments=arguments,
                )
                if rendered_errors:
                    raise ValueError(" | ".join(rendered_errors))
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
        task_payload = {
            "title": title,
            "schedule_type": schedule_type,
            "schedule": schedule,
            "steps": steps,
            "template_text": template_text,
            "metadata": metadata or {},
        }
        contract_errors = validate_scheduler_create_task_payload(task_payload)
        if contract_errors:
            raise ValueError("Invalid scheduler task payload: " + " | ".join(contract_errors))

        clean_schedule_type = str(schedule_type or "").strip().lower()
        validated_schedule = validate_schedule_definition(clean_schedule_type, schedule)
        await self._validate_task_route(steps)

        task_id = str(uuid.uuid4())
        now_value = now_iso()
        task = {
            "task_id": task_id,
            "title": str(title or "").strip() or f"task_{task_id[:8]}",
            "status": "active",
            "created_at": now_value,
            "updated_at": now_value,
            "schedule_type": clean_schedule_type,
            "schedule": validated_schedule,
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
