import os
import uuid
from typing import Any, Awaitable, Callable, Dict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.shared import build_service_logger

from .client import TelegramApiClient
from .service import TelegramMCPService
from .storage import TelegramBindingsStore

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
    data_dir = os.getenv("TELEGRAM_MCP_DATA_DIR", os.path.join(project_root, "core", "telegram_mcp", "data"))
    logs_root = os.getenv("SERVICE_LOGS_DIR", os.path.join(project_root, "logs"))

    logger = build_service_logger("telegram_mcp", logs_root)
    service = TelegramMCPService(
        api_client=TelegramApiClient(),
        storage=TelegramBindingsStore(data_dir),
        logger=logger,
    )

    mcp = FastMCP(
        name="telegram-mcp",
        instructions=(
            "Telegram MCP server. Tools come from the telegram MCP service. "
            "Use resolve_chat_id to find a chat_id by username after the user messages the bot. "
            "Use send_message only for concrete chat_id and non-empty text."
        ),
        log_level="INFO",
    )

    resolve_tool = _wrap_tool(logger, "telegram", "resolve_chat_id", service.resolve_chat_id)
    send_tool = _wrap_tool(logger, "telegram", "send_message", service.send_message)

    @mcp.tool(
        name="resolve_chat_id",
        description="Resolve Telegram chat_id by username. Requires the user to send any message to the bot first.",
    )
    async def resolve_chat_id(username: str, trace_id: str = "") -> Dict[str, Any]:
        return await resolve_tool(username=username, trace_id=trace_id)

    @mcp.tool(
        name="send_message",
        description=(
            "Send a text message to a known Telegram chat_id."
        ),
    )
    async def send_message(chat_id: str, text: str, trace_id: str = "") -> Dict[str, Any]:
        return await send_tool(chat_id=chat_id, text=text, trace_id=trace_id)

    return mcp
