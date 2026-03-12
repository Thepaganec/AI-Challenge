import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.scheduler_mcp.logging_utils import build_scheduler_logger
from core.scheduler_mcp.runtime import SchedulerRuntime
from core.scheduler_mcp.storage import SchedulerStorage
from core.scheduler_mcp.telegram_client import TelegramClient

load_dotenv(override=True)


def create_mcp_server() -> FastMCP:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.getenv("SCHEDULER_MCP_DATA_DIR", os.path.join(project_root, "core", "scheduler_mcp", "data"))
    logs_dir = os.getenv("SCHEDULER_MCP_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_scheduler_logger(logs_dir)
    storage = SchedulerStorage(data_dir)
    telegram_client = TelegramClient()
    runtime = SchedulerRuntime(storage=storage, telegram_client=telegram_client, logger=logger)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        logger.info("Scheduler MCP server starting")
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()
            logger.info("Scheduler MCP server stopped")

    mcp = FastMCP(
        name="local-scheduler-mcp",
        instructions=(
            "Provides tools for Telegram recipient binding and scheduled background tasks. "
            "For personal Telegram delivery, the user must first send any message to the bot, "
            "then telegram_bind_recipient should be called before scheduler_create_task."
        ),
        lifespan=lifespan,
        log_level="INFO",
    )

    @mcp.tool(
        name="telegram_bind_recipient",
        description=(
            "Bind a Telegram recipient by username after the user has sent a message to the bot. "
            "Input requires telegram_username without guessing. If the user is not found in getUpdates, "
            "the tool returns a message asking the user to message the bot first."
        ),
    )
    async def telegram_bind_recipient(telegram_username: str) -> Dict[str, Any]:
        return await runtime.bind_recipient(telegram_username)

    @mcp.tool(
        name="scheduler_create_task",
        description=(
            "Create a scheduled Telegram task. Supports schedule_type: once, interval, daily, weekly. "
            "For once use schedule.run_at='YYYY-MM-DD HH:MM'. "
            "For interval use schedule.every with schedule.unit='minutes' or 'hours'. "
            "For daily use schedule.time_points=[{hour, minute}]. "
            "For weekly use schedule.days plus schedule.time_points. "
            "The telegram recipient must already be bound with telegram_bind_recipient. "
            "Current supported job_type is weather_summary and it sends a fixed stub weather message."
        ),
    )
    async def scheduler_create_task(
        title: str,
        job_type: str,
        job_payload: Dict[str, Any],
        schedule_type: str,
        schedule: Dict[str, Any],
        telegram_username: str,
    ) -> Dict[str, Any]:
        task = await runtime.create_task(
            title=title,
            job_type=job_type,
            job_payload=job_payload,
            schedule_type=schedule_type,
            schedule=schedule,
            telegram_username=telegram_username,
        )
        return {
            "ok": True,
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "schedule_type": task.get("schedule_type"),
            "next_run_at": task.get("next_run_at"),
            "telegram_username": (task.get("recipient") or {}).get("telegram_username"),
            "message": f"Task '{task.get('title')}' created.",
        }

    @mcp.tool(
        name="scheduler_list_tasks",
        description=(
            "List scheduler tasks. Optional filters: status and telegram_username. "
            "Use this to inspect active or existing scheduled Telegram tasks."
        ),
    )
    async def scheduler_list_tasks(status: str = "", telegram_username: str = "") -> Dict[str, Any]:
        tasks = await runtime.list_tasks(status=status, telegram_username=telegram_username)
        return {
            "count": len(tasks),
            "tasks": [
                {
                    "task_id": task.get("task_id"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "schedule_type": task.get("schedule_type"),
                    "next_run_at": task.get("next_run_at"),
                    "telegram_username": ((task.get("recipient") or {}).get("telegram_username") if isinstance(task.get("recipient"), dict) else ""),
                    "job_type": task.get("job_type"),
                }
                for task in tasks
            ],
        }

    @mcp.tool(
        name="scheduler_cancel_task",
        description="Cancel and delete a scheduled task by task_id.",
    )
    async def scheduler_cancel_task(task_id: str) -> Dict[str, Any]:
        result = await runtime.cancel_task(task_id)
        return {"ok": True, **result, "message": f"Task '{task_id}' cancelled."}

    return mcp
