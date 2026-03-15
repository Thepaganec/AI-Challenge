import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List


class RemoteMCPError(RuntimeError):
    pass


@dataclass
class RemoteMCPServer:
    server_name: str
    command: str
    args: List[str]
    env: Dict[str, str]
    timeout_sec: int = 30


class StdioMCPToolClient:
    def __init__(self, logger: Any = None):
        self.logger = logger

    def _log(self, level: str, event: str, payload: Dict[str, Any]) -> None:
        if self.logger is None:
            return
        fn = getattr(self.logger, level, None)
        if callable(fn):
            fn(event, payload)

    async def list_tools(self, server: RemoteMCPServer) -> List[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as e:
            raise RemoteMCPError(f"Python package 'mcp' is not installed: {e}") from e

        params = StdioServerParameters(command=server.command, args=list(server.args), env=dict(server.env))
        self._log("info", "REMOTE_MCP_LIST_TOOLS_REQUEST", {"server": server.server_name, "command": server.command, "args": server.args})
        try:
            async with asyncio.timeout(server.timeout_sec):
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        response = await session.list_tools()
        except Exception as e:
            self._log("error", "REMOTE_MCP_LIST_TOOLS_ERROR", {"server": server.server_name, "error": str(e)})
            raise RemoteMCPError(str(e)) from e

        tools = getattr(response, "tools", None)
        if tools is None and isinstance(response, dict):
            tools = response.get("tools")
        result = list(tools or [])
        self._log("info", "REMOTE_MCP_LIST_TOOLS_RESPONSE", {"server": server.server_name, "tools_count": len(result)})
        return result

    async def call_tool(self, server: RemoteMCPServer, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as e:
            raise RemoteMCPError(f"Python package 'mcp' is not installed: {e}") from e

        params = StdioServerParameters(command=server.command, args=list(server.args), env=dict(server.env))
        payload = arguments if isinstance(arguments, dict) else {}
        self._log(
            "info",
            "REMOTE_MCP_CALL_REQUEST",
            {"server": server.server_name, "tool": tool_name, "arguments": payload, "command": server.command, "args": server.args},
        )
        try:
            async with asyncio.timeout(server.timeout_sec):
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        response = await session.call_tool(tool_name, payload)
        except Exception as e:
            error_payload = {"server": server.server_name, "tool": tool_name, "arguments": payload, "error": str(e)}
            self._log("error", "REMOTE_MCP_CALL_ERROR", error_payload)
            raise RemoteMCPError(str(e)) from e

        normalized = self._normalize_tool_result(response)
        self._log("info", "REMOTE_MCP_CALL_RESPONSE", {"server": server.server_name, "tool": tool_name, "result": normalized})
        return normalized

    def _normalize_tool_result(self, response: Any) -> Dict[str, Any]:
        if isinstance(response, dict):
            return response

        content = getattr(response, "content", None)
        structured_content = getattr(response, "structuredContent", None)
        is_error = bool(getattr(response, "isError", False))
        if isinstance(structured_content, dict):
            if isinstance(structured_content.get("result"), dict):
                payload = dict(structured_content.get("result") or {})
                if is_error and "is_error" not in payload:
                    payload["is_error"] = True
                return payload
            payload = dict(structured_content)
            if is_error and "is_error" not in payload:
                payload["is_error"] = True
            return payload
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                else:
                    text = str(getattr(item, "text", "") or "").strip()
                if not text.startswith("{"):
                    continue
                try:
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        if is_error and "is_error" not in payload:
                            payload["is_error"] = True
                        return payload
                except Exception:
                    continue
        if content is not None:
            return {
                "is_error": is_error,
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
