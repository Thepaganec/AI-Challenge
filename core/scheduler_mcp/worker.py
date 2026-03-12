import asyncio
import os

from dotenv import load_dotenv

from core.scheduler_mcp.logging_utils import build_scheduler_logger
from core.scheduler_mcp.runtime import SchedulerRuntime
from core.scheduler_mcp.storage import SchedulerStorage
from core.scheduler_mcp.telegram_client import TelegramClient

load_dotenv(override=True)


async def run_worker() -> None:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.getenv("SCHEDULER_MCP_DATA_DIR", os.path.join(project_root, "core", "scheduler_mcp", "data"))
    logs_dir = os.getenv("SCHEDULER_MCP_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_scheduler_logger(logs_dir)
    storage = SchedulerStorage(data_dir)
    telegram_client = TelegramClient()
    runtime = SchedulerRuntime(storage=storage, telegram_client=telegram_client, logger=logger)

    await runtime.start()
    logger.info("Scheduler worker is running")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runtime.stop()

