import asyncio
import sys

sys.dont_write_bytecode = True

from core.mcp_orchestrator import build_mcp_server


async def main() -> None:
    server, _runtime = await build_mcp_server()
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
