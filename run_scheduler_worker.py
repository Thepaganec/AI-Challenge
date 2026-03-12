import asyncio, sys
sys.dont_write_bytecode = True

from core.scheduler_mcp.worker import run_worker


if __name__ == "__main__":
    asyncio.run(run_worker())
