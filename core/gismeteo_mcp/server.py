import os
import uuid
from typing import Any, Awaitable, Callable, Dict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.shared import build_service_logger

from .service import GismeteoMCPService

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
    logs_root = os.getenv("SERVICE_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_service_logger("gismeteo_mcp", logs_root)
    service = GismeteoMCPService(
        source_url=str(os.getenv("GISMETEO_URL", "https://www.gismeteo.ru/weather-tver-4327/")).strip(),
        logger=logger,
        timeout_sec=max(5, int(str(os.getenv("GISMETEO_TIMEOUT_SEC", "20")).strip() or "20")),
    )

    mcp = FastMCP(
        name="gismeteo-mcp",
        instructions=(
            "Gismeteo MCP server. It provides exactly one business tool for current weather. "
            "The tool uses local HTML parsing helpers and does not use an external API key."
        ),
        log_level="INFO",
    )

    weather_tool = _wrap_tool(logger, "gismeteo", "get_current_weather", service.fetch_current_weather)

    @mcp.tool(
        name="get_current_weather",
        description=(
            "Returns the current weather parsed from the configured Gismeteo HTML page."
        ),
    )
    async def get_current_weather(trace_id: str = "") -> Dict[str, Any]:
        return await weather_tool(trace_id=trace_id)

    return mcp
