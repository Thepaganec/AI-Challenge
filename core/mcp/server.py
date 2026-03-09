import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from aiohttp import web
from dotenv import load_dotenv

load_dotenv(override=True)


class MCPServerApp:
    def __init__(self) -> None:
        self.server_name = "ai-challenge-mcp"
        self.server_version = "0.1.0"
        self._tools: List[Dict[str, Any]] = [
            {
                "name": "ping",
                "description": "Проверка доступности MCP-сервера.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "echo",
                "description": "Возвращает переданный текст.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Текст для возврата."}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_time",
                "description": "Возвращает текущее UTC-время сервера.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        ]
        self._rpc_handlers = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
        }

    async def handle_rpc(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(self._error(None, -32700, "Parse error"), status=400)

        req_id = payload.get("id")
        method = str(payload.get("method") or "").strip()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

        try:
            handler = self._rpc_handlers.get(method)
            if handler is None:
                return web.json_response(self._error(req_id, -32601, f"Method not found: {method}"), status=404)
            result = await handler(params)
            return web.json_response(self._result(req_id, result))
        except ValueError as e:
            return web.json_response(self._error(req_id, -32602, str(e)), status=400)
        except Exception as e:
            return web.json_response(self._error(req_id, -32000, str(e)), status=500)

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": self.server_name,
                "version": self.server_version,
            },
            "capabilities": {"tools": {"listChanged": False}},
        }

    async def _handle_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True}

    async def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": self._tools}

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call_tool(params)

    async def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not name:
            raise ValueError("tools/call requires 'name'")

        if name == "ping":
            text = "pong"
        elif name == "echo":
            text = str(arguments.get("text") or "")
            if not text:
                raise ValueError("echo requires 'text'")
        elif name == "get_time":
            text = datetime.now(timezone.utc).isoformat()
        else:
            raise ValueError(f"Unknown tool: {name}")

        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _result(self, req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": int(code), "message": str(message)}}


async def run_server() -> None:
    host = os.getenv("MCP_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("MCP_BIND_PORT", "8001") or "8001")
    app_impl = MCPServerApp()
    app = web.Application()
    app.router.add_post("/rpc", app_impl.handle_rpc)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    print(f"MCP server started on http://{host}:{port}/rpc")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
