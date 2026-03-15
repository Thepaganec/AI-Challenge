import os
import uuid
from typing import Any, Awaitable, Callable, Dict, List

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.scheduler_service.runtime import SchedulerRuntime
from core.scheduler_service.storage import SchedulerTaskStore
from core.shared import build_service_logger

load_dotenv(override=True)


def _wrap_tool(logger: Any, service_name: str, tool_name: str, handler: Callable[..., Awaitable[Dict[str, Any]]]):
    async def _wrapped(**kwargs):
        trace_id = str(kwargs.get("trace_id") or uuid.uuid4())
        payload = dict(kwargs)
        payload["trace_id"] = trace_id
        logger.info("MCP_TOOL_REQUEST", {"service": service_name, "tool": tool_name, "arguments": payload})
        try:
            result = await handler(**payload)
            logger.info("MCP_TOOL_RESPONSE", {"service": service_name, "tool": tool_name, "trace_id": trace_id, "result": result})
            return result
        except Exception as e:
            error = {
                "ok": False,
                "is_error": True,
                "error_type": "tool_execution_error",
                "service": service_name,
                "tool": tool_name,
                "trace_id": trace_id,
                "message": str(e),
            }
            logger.error("MCP_TOOL_ERROR", error)
            return error

    return _wrapped


def create_mcp_server() -> FastMCP:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.getenv("SCHEDULER_SERVICE_DATA_DIR", os.path.join(project_root, "core", "scheduler_service", "data"))
    logs_root = os.getenv("SERVICE_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_service_logger("mcp_scheduler", logs_root)
    runtime = SchedulerRuntime(storage=SchedulerTaskStore(data_dir), logger=logger)

    mcp = FastMCP(
        name="scheduler-mcp",
        instructions=(
            "Scheduler control MCP server. It manages scheduler tasks, routes, task memory and task status. "
            "Use the exact create_task contract and never send recipient_name/parameters/functions.* step payloads. "
            "The runtime that executes schedules is a separate service."
        ),
        log_level="INFO",
    )

    async def _create_task(trace_id: str = "", title: str = "", schedule_type: str = "", schedule: Dict[str, Any] | None = None,
                           steps: List[Dict[str, Any]] | None = None, template_text: str = "", metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        task = await runtime.create_task(
            title=title,
            schedule_type=schedule_type,
            schedule=dict(schedule or {}),
            steps=list(steps or []),
            template_text=template_text,
            metadata=dict(metadata or {}),
        )
        return {
            "ok": True,
            "trace_id": trace_id,
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "next_run_at": task.get("next_run_at"),
            "schedule_type": task.get("schedule_type"),
            "steps_count": len(task.get("steps") or []),
            "task": task,
        }

    async def _list_tasks(trace_id: str = "", status: str = "") -> Dict[str, Any]:
        tasks = await runtime.list_tasks(status=status)
        return {"ok": True, "trace_id": trace_id, "count": len(tasks), "tasks": tasks}

    async def _get_task(trace_id: str = "", task_id: str = "") -> Dict[str, Any]:
        task = await runtime.get_task(task_id)
        return {"ok": True, "trace_id": trace_id, "task": task}

    async def _delete_task(trace_id: str = "", task_id: str = "") -> Dict[str, Any]:
        result = await runtime.delete_task(task_id)
        return {"ok": True, "trace_id": trace_id, **result}

    async def _get_task_memory(trace_id: str = "", task_id: str = "") -> Dict[str, Any]:
        task = await runtime.get_task(task_id)
        return {
            "ok": True,
            "trace_id": trace_id,
            "task_id": task_id,
            "execution_memory": task.get("execution_memory") or {},
            "last_run_log": task.get("last_run_log") or [],
            "last_error": task.get("last_error") or "",
        }

    async def _get_scheduler_hints(trace_id: str = "") -> Dict[str, Any]:
        return {
            "ok": True,
            "trace_id": trace_id,
            "summary": (
                "Use this format when creating scheduler tasks. "
                "A task stores schedule_type, schedule and a linear list of steps. "
                "Each step must use only tool, arguments, arguments_template, save_result_as. "
                "Never send recipient_name, parameters or functions.* prefixes. "
                "Later steps can use arguments_template with {{memory_key.field}} placeholders."
            ),
            "schedule_examples": {
                "interval": {"every": 10, "unit": "minutes"},
                "once": {"run_at": "2026-03-15 18:30"},
            },
            "step_schema": {
                "tool": "public orchestrator tool name such as gismeteo__get_current_weather or telegram__send_message",
                "arguments": "optional dict with static arguments",
                "arguments_template": "optional dict rendered from execution memory before the step runs",
                "save_result_as": "optional memory key for storing the full tool result",
            },
            "examples": [
                {
                    "title": "Weather to Telegram every 10 minutes",
                    "schedule_type": "interval",
                    "schedule": {"every": 10, "unit": "minutes"},
                    "steps": [
                        {
                            "tool": "gismeteo__get_current_weather",
                            "arguments": {},
                            "save_result_as": "weather",
                        },
                        {
                            "tool": "telegram__send_message",
                            "arguments_template": {
                                "chat_id": "123456789",
                                "text": "Погода сейчас: {{weather.summary}}",
                            },
                        },
                    ],
                }
            ],
        }

    create_tool = _wrap_tool(logger, "scheduler", "create_task", _create_task)
    list_tool = _wrap_tool(logger, "scheduler", "list_tasks", _list_tasks)
    get_tool = _wrap_tool(logger, "scheduler", "get_task", _get_task)
    delete_tool = _wrap_tool(logger, "scheduler", "delete_task", _delete_task)
    memory_tool = _wrap_tool(logger, "scheduler", "get_task_memory", _get_task_memory)
    hints_tool = _wrap_tool(logger, "scheduler", "get_scheduler_hints", _get_scheduler_hints)

    @mcp.tool(
        name="create_task",
        description=(
            "Create a scheduled task with a generic linear route of steps, optional save_result_as fields, and arguments_template values."
        ),
    )
    async def create_task(title: str, schedule_type: str, schedule: Dict[str, Any], steps: List[Dict[str, Any]], template_text: str = "", metadata: Dict[str, Any] | None = None, trace_id: str = "") -> Dict[str, Any]:
        return await create_tool(title=title, schedule_type=schedule_type, schedule=schedule, steps=steps, template_text=template_text, metadata=metadata or {}, trace_id=trace_id)

    @mcp.tool(
        name="list_tasks",
        description="List scheduler tasks and their current status.",
    )
    async def list_tasks(status: str = "", trace_id: str = "") -> Dict[str, Any]:
        return await list_tool(status=status, trace_id=trace_id)

    @mcp.tool(
        name="get_task",
        description="Return full stored configuration for a task including route and schedule.",
    )
    async def get_task(task_id: str, trace_id: str = "") -> Dict[str, Any]:
        return await get_tool(task_id=task_id, trace_id=trace_id)

    @mcp.tool(
        name="delete_task",
        description="Delete a task by task_id.",
    )
    async def delete_task(task_id: str, trace_id: str = "") -> Dict[str, Any]:
        return await delete_tool(task_id=task_id, trace_id=trace_id)

    @mcp.tool(
        name="get_task_memory",
        description="Return execution memory, last run log and last error for a task.",
    )
    async def get_task_memory(task_id: str, trace_id: str = "") -> Dict[str, Any]:
        return await memory_tool(task_id=task_id, trace_id=trace_id)

    @mcp.tool(
        name="get_scheduler_hints",
        description="Return the canonical scheduler task payload format with schedule and step examples.",
    )
    async def get_scheduler_hints(trace_id: str = "") -> Dict[str, Any]:
        return await hints_tool(trace_id=trace_id)

    return mcp
