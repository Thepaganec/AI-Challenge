import asyncio
import os

from dotenv import load_dotenv

from core.scheduler_service.runtime import SchedulerRuntime
from core.scheduler_service.storage import SchedulerTaskStore
from core.shared import build_service_logger

load_dotenv(override=True)


async def run_worker() -> None:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.getenv("SCHEDULER_SERVICE_DATA_DIR", os.path.join(project_root, "core", "scheduler_service", "data"))
    logs_root = os.getenv("SERVICE_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_service_logger("scheduler_service", logs_root)
    runtime = SchedulerRuntime(storage=SchedulerTaskStore(data_dir), logger=logger)
    await runtime.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runtime.stop()
