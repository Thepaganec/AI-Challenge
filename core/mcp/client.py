import json
import os
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.shared import RemoteMCPError, RemoteMCPServer, StdioMCPToolClient


class MCPClientError(RuntimeError):
    pass


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: Dict[str, Any]

    def to_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class MCPClient:
    def __init__(self, logger: Any = None):
        self.logger = logger
        self.enabled = str(os.getenv("MCP_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        self.transport = str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower() or "stdio"
        self.command = str(os.getenv("MCP_SERVER_COMMAND", "")).strip() or self._default_command()
        self.args = self._parse_args(os.getenv("MCP_SERVER_ARGS", "")) or self._default_args()
        self.timeout_sec = max(1, int(str(os.getenv("MCP_TIMEOUT_SEC", "30")).strip() or "30"))
        self.client = StdioMCPToolClient(logger=logger)

    def _default_command(self) -> str:
        configured = str(os.getenv("PYTHON_EXECUTABLE") or "").strip()
        if configured:
            return configured
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        venv_python = os.path.join(project_root, ".venv", "Scripts", "python.exe")
        if os.path.exists(venv_python):
            return venv_python
        return str(sys.executable).strip() or sys.executable

    def _default_args(self) -> List[str]:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        return [os.path.join(project_root, "run_mcp_orchestrator.py")]

    def _log(self, level: str, message: str, extra: Any = None) -> None:
        if self.logger is None:
            return
        fn = getattr(self.logger, "write", None)
        if not callable(fn):
            return
        if extra is None:
            fn(level.upper(), message)
            return
        if isinstance(extra, str):
            payload = extra
        else:
            payload = json.dumps(extra, ensure_ascii=False, indent=2)
        fn(level.upper(), message, extra=payload)

    def _parse_args(self, raw: str) -> List[str]:
        clean = str(raw or "").strip()
        if not clean:
            return []
        if clean.startswith("["):
            try:
                parsed = json.loads(clean)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except Exception:
                pass
        try:
            return shlex.split(clean, posix=False)
        except Exception:
            return [clean]

    def _server_env(self) -> Dict[str, str]:
        return dict(os.environ)

    def _server_spec(self) -> RemoteMCPServer:
        return RemoteMCPServer(
            server_name="mcp_orchestrator",
            command=self.command,
            args=list(self.args),
            env=self._server_env(),
            timeout_sec=self.timeout_sec,
        )

    def _ensure_configured(self) -> None:
        if not self.enabled:
            raise MCPClientError("MCP disabled by configuration")
        if self.transport != "stdio":
            raise MCPClientError(f"Unsupported MCP transport: {self.transport}")
        if not self.command:
            raise MCPClientError("MCP_SERVER_COMMAND is not configured")

    async def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "connected": False,
                "transport": self.transport,
                "command": self.command,
                "args": self.args,
                "error": "MCP disabled",
            }
        try:
            tools = await self.list_tools()
            return {
                "enabled": True,
                "connected": True,
                "transport": self.transport,
                "command": self.command,
                "args": self.args,
                "tools_count": len(tools),
            }
        except Exception as e:
            return {
                "enabled": True,
                "connected": False,
                "transport": self.transport,
                "command": self.command,
                "args": self.args,
                "error": str(e),
            }

    async def list_tools(self) -> List[MCPTool]:
        self._ensure_configured()
        try:
            tools_raw = await self.client.list_tools(self._server_spec())
        except RemoteMCPError as e:
            self._log("error", "MCP_TOOLS_LIST_ERROR", {"error": str(e)})
            raise MCPClientError(str(e)) from e

        tools: List[MCPTool] = []
        for item in tools_raw or []:
            name = str(getattr(item, "name", "") or (item.get("name") if isinstance(item, dict) else "")).strip()
            if not name:
                continue
            description = str(getattr(item, "description", "") or (item.get("description") if isinstance(item, dict) else "")).strip()
            schema = getattr(item, "inputSchema", None)
            if schema is None and isinstance(item, dict):
                schema = item.get("inputSchema") or item.get("input_schema")
            if schema is None:
                schema = getattr(item, "input_schema", None)
            tools.append(MCPTool(name=name, description=description, input_schema=schema if isinstance(schema, dict) else {}))
        self._log("info", "MCP_TOOLS_LIST", {"tools": [tool.name for tool in tools]})
        return tools

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure_configured()
        tool_name = str(name or "").strip()
        if not tool_name:
            raise MCPClientError("tool name is required")
        payload = arguments if isinstance(arguments, dict) else {}
        self._log("info", "MCP_TOOL_CALL_START", {"name": tool_name, "arguments": payload})
        try:
            result = await self.client.call_tool(self._server_spec(), tool_name, payload)
        except RemoteMCPError as e:
            self._log("error", "MCP_TOOL_CALL_ERROR", {"name": tool_name, "error": str(e)})
            raise MCPClientError(str(e)) from e
        self._log("info", "MCP_TOOL_CALL_RESULT", {"name": tool_name, "result": result})
        return result
