import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


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
        self.command = str(os.getenv("MCP_SERVER_COMMAND", "")).strip()
        self.args = self._parse_args(os.getenv("MCP_SERVER_ARGS", ""))
        self.timeout_sec = max(1, int(str(os.getenv("MCP_TIMEOUT_SEC", "30")).strip() or "30"))

    def _log(self, level: str, message: str, extra: Any = None) -> None:
        if self.logger is None:
            return
        if extra is None:
            self.logger.write(level, message)
            return
        if isinstance(extra, str):
            payload = extra
        else:
            payload = json.dumps(extra, ensure_ascii=False, indent=2)
        self.logger.write(level, message, extra=payload)

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
        env = dict(os.environ)
        if os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN"):
            env["GITHUB_PERSONAL_ACCESS_TOKEN"] = str(os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN"))
        if os.getenv("GITHUB_PAT"):
            env["GITHUB_PAT"] = str(os.getenv("GITHUB_PAT"))
        env["GITHUB_READ_ONLY"] = str(os.getenv("GITHUB_READ_ONLY", "1"))
        if os.getenv("GITHUB_TOOLSETS"):
            env["GITHUB_TOOLSETS"] = str(os.getenv("GITHUB_TOOLSETS"))
        return env

    def _ensure_configured(self) -> None:
        if not self.enabled:
            raise MCPClientError("MCP disabled by configuration")
        if self.transport != "stdio":
            raise MCPClientError(f"Unsupported MCP transport: {self.transport}")
        if not self.command:
            raise MCPClientError("MCP_SERVER_COMMAND is not configured")

    async def _with_session(self):
        self._ensure_configured()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as e:
            raise MCPClientError(f"Python package 'mcp' is not installed: {e}") from e

        params = StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=self._server_env(),
        )
        return ClientSession, stdio_client, params

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
        ClientSession, stdio_client, params = await self._with_session()
        try:
            async with asyncio.timeout(self.timeout_sec):
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        response = await session.list_tools()
        except Exception as e:
            self._log("ERROR", "MCP_TOOLS_LIST_ERROR", {"error": str(e)})
            raise MCPClientError(str(e)) from e

        tools_raw = getattr(response, "tools", None)
        if tools_raw is None and isinstance(response, dict):
            tools_raw = response.get("tools")

        tools: List[MCPTool] = []
        for item in tools_raw or []:
            name = str(getattr(item, "name", "") or (item.get("name") if isinstance(item, dict) else "")).strip()
            if not name:
                continue
            description = str(
                getattr(item, "description", "") or (item.get("description") if isinstance(item, dict) else "")
            ).strip()
            schema = getattr(item, "inputSchema", None)
            if schema is None and isinstance(item, dict):
                schema = item.get("inputSchema") or item.get("input_schema")
            if schema is None:
                schema = getattr(item, "input_schema", None)
            tools.append(MCPTool(name=name, description=description, input_schema=schema if isinstance(schema, dict) else {}))
        self._log("INFO", "MCP_TOOLS_LIST", {"tools": [tool.name for tool in tools]})
        return tools

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        tool_name = str(name or "").strip()
        if not tool_name:
            raise MCPClientError("tool name is required")

        ClientSession, stdio_client, params = await self._with_session()
        payload = arguments if isinstance(arguments, dict) else {}
        self._log("INFO", "MCP_TOOL_CALL_START", {"name": tool_name, "arguments": payload})
        try:
            async with asyncio.timeout(self.timeout_sec):
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        response = await session.call_tool(tool_name, payload)
        except Exception as e:
            self._log("ERROR", "MCP_TOOL_CALL_ERROR", {"name": tool_name, "error": str(e)})
            raise MCPClientError(str(e)) from e

        result = self._normalize_tool_result(response)
        self._log("INFO", "MCP_TOOL_CALL_RESULT", {"name": tool_name, "result": result})
        return result

    def _normalize_tool_result(self, response: Any) -> Dict[str, Any]:
        if isinstance(response, dict):
            return response

        content = getattr(response, "content", None)
        structured_content = getattr(response, "structuredContent", None)
        is_error = bool(getattr(response, "isError", False))
        if structured_content is not None or content is not None:
            return {
                "is_error": is_error,
                "structured_content": structured_content,
                "content": self._normalize_content_blocks(content),
            }
        return {"is_error": is_error, "content": str(response)}

    def _normalize_content_blocks(self, content: Any) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        if not isinstance(content, list):
            if content is None:
                return blocks
            return [{"type": "text", "text": str(content)}]

        for item in content:
            if isinstance(item, dict):
                blocks.append(item)
                continue
            block_type = str(getattr(item, "type", "") or "text").strip() or "text"
            entry: Dict[str, Any] = {"type": block_type}
            for key in ("text", "data", "mimeType", "mime_type", "url"):
                value = getattr(item, key, None)
                if value is not None:
                    entry[key] = value
            if len(entry) == 1:
                entry["text"] = str(item)
            blocks.append(entry)
        return blocks
