import os
import uuid
from typing import Any, Awaitable, Callable, Dict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.shared import build_service_logger

from .service import ProjectMCPService

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
    logger = build_service_logger("mcp_project", logs_root)
    service = ProjectMCPService(project_root=project_root, logger=logger)

    mcp = FastMCP(
        name="project-mcp",
        instructions="Project MCP server. Provides repository context such as git branch and a compact file overview.",
        log_level="INFO",
    )

    git_branch_tool = _wrap_tool(logger, "project", "get_git_branch", service.get_git_branch)
    list_files_tool = _wrap_tool(logger, "project", "list_project_files", service.list_project_files)

    @mcp.tool(
        name="get_git_branch",
        description="Returns the current git branch for the local repository.",
    )
    async def get_git_branch(trace_id: str = "") -> Dict[str, Any]:
        return await git_branch_tool(trace_id=trace_id)

    @mcp.tool(
        name="list_project_files",
        description="Returns a compact overview of project files for architecture questions.",
    )
    async def list_project_files(limit: int = 60, max_depth: int = 3, trace_id: str = "") -> Dict[str, Any]:
        return await list_files_tool(limit=limit, max_depth=max_depth, trace_id=trace_id)

    return mcp
