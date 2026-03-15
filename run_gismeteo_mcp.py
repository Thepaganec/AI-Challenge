import asyncio
import sys

sys.dont_write_bytecode = True

from core.gismeteo_mcp import create_mcp_server


async def main() -> None:
    server = create_mcp_server()
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
