import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp


class MCPClient:
    def __init__(self, server_url: str, timeout_sec: int = 15, auth_token: str = ""):
        self.server_url = server_url.rstrip("/")
        self.rpc_url = f"{self.server_url}/rpc"
        self.timeout_sec = int(timeout_sec)
        self.auth_token = str(auth_token or "").strip()

        self.connected: bool = False
        self.last_error: str = ""
        self.last_tools: List[Dict[str, Any]] = []

    async def connect(self) -> bool:
        try:
            await self._rpc("initialize", {"clientInfo": {"name": "ai-challenge-agent", "version": "0.1.0"}})
            self.connected = True
            self.last_error = ""
            return True
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            return False

    async def ping(self) -> bool:
        try:
            await self._rpc("ping", {})
            self.connected = True
            self.last_error = ""
            return True
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            return False

    async def list_tools(self, use_cached_fallback: bool = True) -> Dict[str, Any]:
        try:
            payload = await self._rpc("tools/list", {})
            tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
            self.last_tools = tools
            self.connected = True
            self.last_error = ""
            return {"connected": True, "tools": tools, "source": "live", "error": ""}
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            if use_cached_fallback and self.last_tools:
                return {
                    "connected": False,
                    "tools": list(self.last_tools),
                    "source": "cached_fallback",
                    "error": self.last_error,
                }
            return {"connected": False, "tools": [], "source": "live", "error": self.last_error}

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        payload = await self._rpc("tools/call", {"name": str(tool_name or "").strip(), "arguments": args})
        self.connected = True
        self.last_error = ""
        return payload

    def status(self) -> Dict[str, Any]:
        return {
            "connected": bool(self.connected),
            "server_url": self.server_url,
            "last_error": self.last_error,
            "cached_tools_count": int(len(self.last_tools)),
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        body = {
            "jsonrpc": "2.0",
            "id": f"{method}:{datetime.now(timezone.utc).timestamp()}",
            "method": method,
            "params": params if isinstance(params, dict) else {},
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.rpc_url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"MCP HTTP {resp.status}: {text}")
                try:
                    obj = json.loads(text)
                except Exception:
                    raise RuntimeError("MCP invalid JSON response")

        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
        result = obj.get("result")
        if not isinstance(result, dict):
            return {}
        return result

