import json
import os
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Dict, List

from core.shared import RemoteMCPServer


@dataclass
class ServiceRegistryEntry:
    public_name: str
    server: RemoteMCPServer


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _default_python() -> str:
    configured = str(os.getenv("PYTHON_EXECUTABLE") or "").strip()
    if configured:
        return configured
    root = _project_root()
    venv_python = os.path.join(root, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        return venv_python
    return str(sys.executable).strip() or sys.executable


def _default_registry_payload() -> List[Dict[str, Any]]:
    root = _project_root()
    py = _default_python()
    common_env = dict(os.environ)
    return [
        {
            "public_name": "project",
            "command": py,
            "args": [os.path.join(root, "run_mcp_project.py")],
            "env": common_env,
        },
        {
            "public_name": "telegram",
            "command": py,
            "args": [os.path.join(root, "run_mcp_telegram.py")],
            "env": common_env,
        },
        {
            "public_name": "gismeteo",
            "command": py,
            "args": [os.path.join(root, "run_mcp_gismeteo.py")],
            "env": common_env,
        },
        {
            "public_name": "scheduler",
            "command": py,
            "args": [os.path.join(root, "run_mcp_scheduler.py")],
            "env": common_env,
        },
    ]


def _parse_args(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
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


def load_registry() -> List[ServiceRegistryEntry]:
    raw = str(os.getenv("MCP_ORCHESTRATOR_SERVERS", "")).strip()
    payload = _default_registry_payload()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                payload = parsed
        except Exception:
            pass

    entries: List[ServiceRegistryEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        public_name = str(item.get("public_name") or item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not public_name or not command:
            continue
        env = dict(os.environ)
        if isinstance(item.get("env"), dict):
            env.update({str(key): str(value) for key, value in item.get("env", {}).items()})
        entries.append(
            ServiceRegistryEntry(
                public_name=public_name,
                server=RemoteMCPServer(
                    server_name=public_name,
                    command=command,
                    args=_parse_args(item.get("args")),
                    env=env,
                    timeout_sec=max(1, int(item.get("timeout_sec") or os.getenv("MCP_TIMEOUT_SEC", "30"))),
                ),
            )
        )
    return entries

