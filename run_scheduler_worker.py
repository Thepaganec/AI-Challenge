import asyncio

from core.scheduler_service.worker import run_worker

if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
