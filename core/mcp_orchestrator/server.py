import keyword
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.shared import StdioMCPToolClient, build_service_logger

from .registry import ServiceRegistryEntry, load_registry

load_dotenv(override=True)


@dataclass
class ToolRoute:
    public_name: str
    logical_name: str
    source_name: str
    service_name: str
    description: str
    input_schema: Dict[str, Any]


class OrchestratorRuntime:
    def __init__(self, registry: List[ServiceRegistryEntry], logger: Any):
        self.registry = registry
        self.logger = logger
        self.client = StdioMCPToolClient(logger=logger)
        self.routes_by_public: Dict[str, ToolRoute] = {}
        self.routes_by_logical: Dict[str, ToolRoute] = {}
        self.service_map: Dict[str, ServiceRegistryEntry] = {entry.public_name: entry for entry in registry}

    async def load_routes(self) -> List[ToolRoute]:
        public_routes: Dict[str, ToolRoute] = {}
        logical_routes: Dict[str, ToolRoute] = {}
        for entry in self.registry:
            tools = await self.client.list_tools(entry.server)
            for tool in tools:
                source_name = str(getattr(tool, "name", "") or (tool.get("name") if isinstance(tool, dict) else "")).strip()
                if not source_name:
                    continue
                source_description = str(
                    getattr(tool, "description", "") or (tool.get("description") if isinstance(tool, dict) else "")
                ).strip()
                schema = getattr(tool, "inputSchema", None)
                if schema is None and isinstance(tool, dict):
                    schema = tool.get("inputSchema") or tool.get("input_schema")
                if schema is None:
                    schema = getattr(tool, "input_schema", None)
                logical_name = f"{entry.public_name}.{source_name}"
                public_name = f"{entry.public_name}__{source_name}"
                description = f"{entry.public_name}: {source_description}".strip()
                route = ToolRoute(
                    public_name=public_name,
                    logical_name=logical_name,
                    source_name=source_name,
                    service_name=entry.public_name,
                    description=description,
                    input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                )
                public_routes[public_name] = route
                logical_routes[logical_name] = route
        self.routes_by_public = public_routes
        self.routes_by_logical = logical_routes
        self.logger.info(
            "ORCHESTRATOR_ROUTES_READY",
            {
                "services": [entry.public_name for entry in self.registry],
                "tools": [name for name in sorted(public_routes.keys())],
            },
        )
        return [public_routes[name] for name in sorted(public_routes.keys())]

    def _resolve_route(self, tool_name: str) -> ToolRoute:
        clean = str(tool_name or "").strip()
        route = self.routes_by_public.get(clean)
        if route is not None:
            return route
        route = self.routes_by_logical.get(clean)
        if route is not None:
            return route
        if "__" in clean and "." not in clean:
            fallback = clean.replace("__", ".", 1)
            route = self.routes_by_logical.get(fallback)
            if route is not None:
                return route
        raise ValueError(f"Unknown orchestrated tool: {clean}")

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        route = self._resolve_route(tool_name)
        entry = self.service_map.get(route.service_name)
        if entry is None:
            raise ValueError(f"Service '{route.service_name}' is not registered")
        trace_id = str(arguments.get("trace_id") or "").strip()
        payload = dict(arguments)
        if not trace_id:
            import uuid
            trace_id = str(uuid.uuid4())
            payload["trace_id"] = trace_id
        self.logger.info(
            "ORCHESTRATOR_TOOL_REQUEST",
            {
                "service": route.service_name,
                "public_tool_name": route.public_name,
                "logical_tool_name": route.logical_name,
                "source_tool_name": route.source_name,
                "trace_id": trace_id,
                "arguments": payload,
            },
        )
        result = await self.client.call_tool(entry.server, route.source_name, payload)
        self.logger.info(
            "ORCHESTRATOR_TOOL_RESPONSE",
            {
                "service": route.service_name,
                "public_tool_name": route.public_name,
                "logical_tool_name": route.logical_name,
                "source_tool_name": route.source_name,
                "trace_id": trace_id,
                "result": result,
            },
        )
        return result


def _safe_param_name(name: str) -> str:
    candidate = ''.join(ch if ch.isalnum() or ch == '_' else '_' for ch in str(name or '').strip())
    if not candidate:
        candidate = 'arg'
    if candidate[0].isdigit() or keyword.iskeyword(candidate):
        candidate = f'param_{candidate}'
    return candidate


def _schema_type_to_annotation(schema_type: Any):
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "string")
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    return mapping.get(str(schema_type or "string"), Any)


def _build_proxy_function(route: ToolRoute, runtime: OrchestratorRuntime, logger: Any):
    schema = route.input_schema if isinstance(route.input_schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []

    ordered_names: List[str] = []
    for name in required:
        if isinstance(name, str) and name in properties and name not in ordered_names:
            ordered_names.append(name)
    for name in properties.keys():
        if name not in ordered_names:
            ordered_names.append(name)
    if "trace_id" not in ordered_names:
        ordered_names.append("trace_id")

    params: List[str] = []
    payload_lines: List[str] = []
    for name in ordered_names:
        param_name = _safe_param_name(name)
        is_required = name in required and name != "trace_id"
        params.append(param_name if is_required else f"{param_name}=None")
        payload_lines.append(f'        "{name}": {param_name},')

    params_code = ", ".join(params) if params else ""
    payload_code = "\n".join(payload_lines)
    fn_code = f"async def generated_proxy({params_code}):\n    payload = {{\n{payload_code}\n    }}\n    payload = {{key: value for key, value in payload.items() if value is not None}}\n    try:\n        return await runtime.call_tool('{route.public_name}', payload)\n    except Exception as e:\n        error = {{'ok': False, 'is_error': True, 'error_type': 'orchestrator_routing_error', 'service': '{route.service_name}', 'tool': '{route.public_name}', 'message': str(e), 'trace_id': str(payload.get('trace_id') or '')}}\n        logger.error('ORCHESTRATOR_TOOL_ERROR', error)\n        return error\n"
    namespace = {"runtime": runtime, "logger": logger}
    exec(fn_code, namespace)
    generated = namespace["generated_proxy"]
    generated.__annotations__ = {}
    for name in ordered_names:
        param_name = _safe_param_name(name)
        schema_row = properties.get(name) if isinstance(properties.get(name), dict) else {}
        generated.__annotations__[param_name] = _schema_type_to_annotation(schema_row.get("type") if isinstance(schema_row, dict) else "string")
    generated.__annotations__["return"] = dict
    generated.__name__ = route.public_name
    generated.__qualname__ = route.public_name
    generated.__doc__ = route.description
    return generated


async def build_mcp_server() -> Tuple[FastMCP, OrchestratorRuntime]:
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    logs_root = os.getenv("SERVICE_LOGS_DIR", os.path.join(project_root, "logs"))
    logger = build_service_logger("mcp_orchestrator", logs_root)
    runtime = OrchestratorRuntime(load_registry(), logger)
    routes = await runtime.load_routes()

    mcp = FastMCP(
        name="mcp-orchestrator",
        instructions="Routes tools from registered MCP services.",
        log_level="INFO",
    )

    for route in routes:
        proxy_fn = _build_proxy_function(route, runtime, logger)
        mcp.add_tool(proxy_fn, name=route.public_name, description=route.description)

    return mcp, runtime
