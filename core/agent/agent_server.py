import asyncio
import json
import os
import signal
import sys
import traceback
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from core.LLM_API.gptmodel import GPTModel
from core.agent.agent_logger import AgentFileLogger
from core.agent.invariants import build_invariants_system_text, normalize_invariants_state
from core.agent.memory_store import AgentMemoryStore
from core.agent.profile_store import AgentProfileStore
from core.agent.strategies import (
    build_facts_strategy,
    build_sliding_window,
    build_summary_strategy,
    parse_facts_and_strip_user_text,
)
from core.mcp import MCPClient, MCPClientError

load_dotenv(override=True)


class LLMAgentServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        api_key_env: str = "PROXYAPI_KEY",
        base_url: str = "https://openai.api.proxyapi.ru/v1",
        timeout_sec: int = 60,
    ):
        self.host = host
        self.port = port
        self.base_dir = os.path.dirname(__file__)
        self.project_root = os.path.abspath(os.path.join(self.base_dir, "..", ".."))
        self.logger = AgentFileLogger(logs_dir=self.base_dir, prefix="agentlogs")
        self.logger.cleanup_old_logs(keep_days=3)

        self.memory_dir = os.path.join(self.base_dir, "memory")
        self.memory_store = AgentMemoryStore(base_dir=self.memory_dir)
        self.profile_store = AgentProfileStore(file_path=os.path.join(self.base_dir, "profiles.json"))
        self.gpt = GPTModel(api_key_env=api_key_env, base_url=base_url, timeout_sec=timeout_sec, logger=self.logger)
        self.mcp = MCPClient(logger=self.logger)
        self.scheduler_worker_process: Optional[asyncio.subprocess.Process] = None

        self.pricing_cache: Dict[str, Dict[str, float]] = {}
        self._model_context_limit: Dict[str, int] = {
            "gpt-3.5-turbo": 16384,
            "gpt-4o-mini": 128000,
            "gpt-4o": 128000,
            "gpt-5.2-chat-latest": 400000,
        }
        self._action_handlers: Dict[str, Callable[[Dict[str, Any], asyncio.StreamWriter], Awaitable[None]]] = {}
        self._action_handlers = {
            "ping": self._handle_ping,
            "list_sessions": self._handle_list_sessions,
            "get_session": self._handle_get_session,
            "reset_session": self._handle_reset_session,
            "list_branches": self._handle_list_branches,
            "switch_branch": self._handle_switch_branch,
            "set_active_branch": self._handle_switch_branch,
            "list_checkpoints": self._handle_list_checkpoints,
            "create_checkpoint": self._handle_create_checkpoint,
            "create_branch": self._handle_create_branch,
            "get_memory": self._handle_get_memory,
            "save_memory": self._handle_save_memory,
            "get_profile": self._handle_get_profile,
            "save_profile": self._handle_save_profile,
            "delete_profile": self._handle_delete_profile,
            "set_active_profile": self._handle_set_active_profile,
            "get_profile_state": self._handle_get_profile_state,
            "list_profiles": self._handle_get_profile_state,
            "get_invariants_state": self._handle_get_invariants_state,
            "save_invariant": self._handle_save_invariant,
            "set_invariant_policy": self._handle_set_invariant_policy,
            "mcp_status": self._handle_mcp_status,
            "mcp_list_tools": self._handle_mcp_list_tools,
            "mcp_call_tool": self._handle_mcp_call_tool,
            "stream_chat": self._handle_stream_chat,
        }

    async def _ensure_scheduler_worker(self) -> None:
        enabled = str(os.getenv("SCHEDULER_WORKER_AUTOSTART", "true")).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            self._log("INFO", "SCHEDULER_WORKER_AUTOSTART_DISABLED")
            return
        if self.scheduler_worker_process is not None and self.scheduler_worker_process.returncode is None:
            return

        worker_command = str(os.getenv("SCHEDULER_WORKER_COMMAND") or sys.executable).strip() or sys.executable
        worker_script = str(os.getenv("SCHEDULER_WORKER_SCRIPT") or os.path.join(self.project_root, "run_scheduler_worker.py")).strip()
        worker_args = [worker_script]
        self._log("INFO", "SCHEDULER_WORKER_START", {"command": worker_command, "args": worker_args})
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))
        self.scheduler_worker_process = await asyncio.create_subprocess_exec(
            worker_command,
            *worker_args,
            cwd=self.project_root,
            env=dict(os.environ),
            creationflags=creationflags,
        )
        await asyncio.sleep(0.5)
        if self.scheduler_worker_process.returncode is not None:
            raise RuntimeError(f"Scheduler worker exited immediately with code {self.scheduler_worker_process.returncode}")
        self._log("SUCCESS", "SCHEDULER_WORKER_STARTED", {"pid": self.scheduler_worker_process.pid})

    async def _stop_scheduler_worker(self) -> None:
        proc = self.scheduler_worker_process
        self.scheduler_worker_process = None
        if proc is None or proc.returncode is not None:
            return
        self._log("INFO", "SCHEDULER_WORKER_STOP", {"pid": proc.pid})
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        self._log("INFO", "SCHEDULER_WORKER_STOPPED", {"returncode": proc.returncode})

    async def preload_pricing(self) -> None:
        try:
            self.logger.write("INFO", "Загрузка тарифов ProxyAPI")
            self.pricing_cache = await self.gpt.get_pricing_rub_per_1m()
            self.logger.write("SUCCESS", "Тарифы загружены", extra=f"models={len(self.pricing_cache)}")
        except Exception as e:
            self.logger.write("WARN", "Не удалось загрузить тарифы ProxyAPI", extra=str(e))
            self.pricing_cache = {}

    async def _send_json(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _send_json_maybe_chunked(self, writer: asyncio.StreamWriter, payload: Dict[str, Any], *, max_line_bytes: int = 60000) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) <= max_line_bytes:
            writer.write(raw)
            await writer.drain()
            return
        text = json.dumps(payload, ensure_ascii=False)
        part_size = max(1000, max_line_bytes - 2000)
        parts = [text[i:i + part_size] for i in range(0, len(text), part_size)]
        await self._send_json(writer, {"type": "chunked_start", "orig_type": payload.get("type"), "chunks": len(parts)})
        for idx, part in enumerate(parts):
            await self._send_json(writer, {"type": "chunked_part", "orig_type": payload.get("type"), "i": idx, "data": part})
        await self._send_json(writer, {"type": "chunked_end", "orig_type": payload.get("type")})

    async def _send_error(self, writer: asyncio.StreamWriter, message: str) -> None:
        await self._send_json(writer, {"type": "error", "message": message})

    def _log(self, level: str, message: str, payload: Any = None) -> None:
        if payload is None:
            self.logger.write(level, message)
            return
        if isinstance(payload, str):
            extra = payload
        else:
            extra = json.dumps(payload, ensure_ascii=False, indent=2)
        self.logger.write(level, message, extra=extra)

    def _estimate_tokens_text(self, text: Any) -> int:
        clean = str(text or "").strip()
        return max(1, int(len(clean) / 4)) if clean else 0

    def _estimate_tokens_messages(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            total += self._estimate_tokens_text(msg.get("content"))
            total += 4
        return total

    def _resolve_context_limit(self, model: str) -> int:
        return int(self._model_context_limit.get(str(model or "").strip(), 128000))

    def _calc_cost_rub(self, model_id: str, usage: Dict[str, Any]) -> float:
        row = self.pricing_cache.get(str(model_id or "").strip()) if isinstance(self.pricing_cache, dict) else None
        if not isinstance(row, dict):
            return 0.0
        prompt_tokens = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        in_price = float(row.get("in") or 0.0)
        out_price = float(row.get("out") or 0.0)
        return (prompt_tokens / 1_000_000.0) * in_price + (completion_tokens / 1_000_000.0) * out_price

    def _merge_usage(self, left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(left or {})
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "total_tokens"):
            merged[key] = int(merged.get(key) or 0) + int((right or {}).get(key) or 0)
        return merged

    def _copy_memory_layers(self, layers: Any) -> Dict[str, Any]:
        if not isinstance(layers, dict):
            return {"short_term": [], "working": {}, "long_term": {}}
        return {
            "short_term": list(layers.get("short_term")) if isinstance(layers.get("short_term"), list) else [],
            "working": dict(layers.get("working")) if isinstance(layers.get("working"), dict) else {},
            "long_term": dict(layers.get("long_term")) if isinstance(layers.get("long_term"), dict) else {},
        }

    def _ensure_branch(self, session: Dict[str, Any], branch_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        session["branches"] = branches
        active = str(session.get("active_branch") or "main").strip() or "main"
        bid = str(branch_id or active).strip() or "main"
        if bid not in branches or not isinstance(branches.get(bid), dict):
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            branches[bid] = {
                "branch_id": bid,
                "title": bid,
                "history": [],
                "facts": {},
                "checkpoints": [],
                "summary": "",
                "memory_layers": {"short_term": [], "working": {}, "long_term": {}},
                "created_at": now,
                "updated_at": now,
            }
        branch = branches[bid]
        if not isinstance(branch.get("history"), list):
            branch["history"] = []
        if not isinstance(branch.get("facts"), dict):
            branch["facts"] = {}
        if not isinstance(branch.get("checkpoints"), list):
            branch["checkpoints"] = []
        if not isinstance(branch.get("summary"), str):
            branch["summary"] = ""
        branch["memory_layers"] = self._copy_memory_layers(branch.get("memory_layers"))
        session["active_branch"] = bid
        return bid, branch

    def _make_checkpoint(self, history: List[Dict[str, Any]], name: str = "") -> Dict[str, Any]:
        cp_name = str(name or "").strip() or f"checkpoint_{len(history)}"
        return {
            "id": str(uuid.uuid4()),
            "name": cp_name,
            "cut": int(len(history)),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _create_branch_from_checkpoint(self, session: Dict[str, Any], from_branch_id: str, checkpoint_id: str, new_branch_name: str = "") -> str:
        _, source = self._ensure_branch(session, from_branch_id)
        checkpoints = source.get("checkpoints") if isinstance(source.get("checkpoints"), list) else []
        checkpoint = next((cp for cp in checkpoints if isinstance(cp, dict) and str(cp.get("id")) == str(checkpoint_id)), None)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint not found")
        cut = int(checkpoint.get("cut") or 0)
        history = source.get("history") if isinstance(source.get("history"), list) else []
        bid = str(uuid.uuid4())[:8]
        title = str(new_branch_name or "").strip() or f"branch_{bid}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["branches"][bid] = {
            "branch_id": bid,
            "title": title,
            "history": list(history[:cut]),
            "facts": dict(source.get("facts") or {}),
            "checkpoints": [],
            "summary": str(source.get("summary") or ""),
            "memory_layers": self._copy_memory_layers(source.get("memory_layers")),
            "created_at": now,
            "updated_at": now,
        }
        session["active_branch"] = bid
        return bid

    def _normalize_tool_result_for_history(self, payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _sync_short_term_from_history(self, memory_layers: Dict[str, Any], history: List[Dict[str, Any]], keep_last_items: int = 12) -> None:
        recent: List[str] = []
        for msg in history[-keep_last_items:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            content = msg.get("content")
            if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                names = [str((item.get("function") or {}).get("name") or "") for item in msg.get("tool_calls") if isinstance(item, dict)]
                if names:
                    recent.append(f"assistant_tool_calls: {', '.join(name for name in names if name)}")
                continue
            if content is None:
                continue
            text = str(content).replace("\n", " ").strip()
            if not text:
                continue
            if len(text) > 240:
                text = text[:237].rstrip() + "..."
            recent.append(f"{role}: {text}")
        memory_layers["short_term"] = recent[-keep_last_items:]

    def _save_memory_item(self, memory_layers: Dict[str, Any], layer: str, key: str, value: str) -> None:
        clean_layer = str(layer or "").strip()
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if not clean_value:
            return
        if clean_layer == "short_term":
            bucket = memory_layers.get("short_term") if isinstance(memory_layers.get("short_term"), list) else []
            bucket.append(clean_value)
            memory_layers["short_term"] = bucket[-20:]
            return
        if clean_layer in ("working", "long_term"):
            mapping = memory_layers.get(clean_layer) if isinstance(memory_layers.get(clean_layer), dict) else {}
            mapping[clean_key or f"item_{len(mapping) + 1}"] = clean_value
            memory_layers[clean_layer] = mapping

    def _build_memory_system_text(self, memory_layers: Dict[str, Any]) -> Optional[str]:
        if not isinstance(memory_layers, dict):
            return None
        lines: List[str] = []
        if isinstance(memory_layers.get("working"), dict) and memory_layers.get("working"):
            lines.append("[MEMORY_WORKING]")
            for key, value in memory_layers["working"].items():
                lines.append(f"- {key}: {value}")
        if isinstance(memory_layers.get("long_term"), dict) and memory_layers.get("long_term"):
            lines.append("[MEMORY_LONG_TERM]")
            for key, value in memory_layers["long_term"].items():
                lines.append(f"- {key}: {value}")
        if not lines:
            return None
        return "\n".join(lines)

    def _build_profile_system_text(self, profile_name: str, profile_description: str) -> Optional[str]:
        clean_name = str(profile_name or "").strip()
        clean_desc = str(profile_description or "").strip()
        if not clean_name or not clean_desc:
            return None
        return f"[PROFILE]\n- name: {clean_name}\n- description: {clean_desc}"

    def _merge_system_text(self, *parts: Optional[str]) -> Optional[str]:
        cleaned = [str(part).strip() for part in parts if str(part or "").strip()]
        return "\n\n".join(cleaned) if cleaned else None

    def _build_messages_for_llm(self, *, system_text: Optional[str], history_for_llm: List[Dict[str, Any]], user_text: str) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for item in history_for_llm:
            if isinstance(item, dict):
                messages.append(dict(item))
        messages.append({"role": "user", "content": user_text})
        return messages

    async def _validate_invariants(self, *, req_id: str, messages: List[Dict[str, Any]], invariants_text: Optional[str], model: str) -> Dict[str, Any]:
        if not invariants_text:
            return {"decision": "pass", "reason": "no invariants"}
        validator_messages = [
            {
                "role": "system",
                "content": (
                    "You validate whether the planned assistant response context respects invariants.\n"
                    "Return strict JSON only: {\"decision\":\"pass|warn\",\"reason\":\"...\"}.\n"
                    "Do not call tools."
                ),
            },
            {"role": "user", "content": f"{invariants_text}\n\nConversation snapshot:\n{json.dumps(messages, ensure_ascii=False)}"},
        ]
        try:
            result = await self.gpt.complete_text(
                messages=validator_messages,
                model=model,
                max_tokens=200,
                temperature=0.0,
                trace_id=f"{req_id}:invariants",
            )
            text = str(result.get("text") or "").strip()
            payload = json.loads(text) if text.startswith("{") else {}
            decision = str(payload.get("decision") or "pass").strip().lower()
            if decision not in {"pass", "warn"}:
                decision = "warn"
            return {"decision": decision, "reason": str(payload.get("reason") or text)}
        except Exception as e:
            return {"decision": "warn", "reason": f"invariants validator failed: {e}"}

    def _serialize_mcp_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        return [{"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in tools]

    def _build_tool_usage_instruction(self, tools: List[Any]) -> Optional[str]:
        if not tools:
            return None
        lines = [
            "[TOOLS_POLICY]\n"
            "- Use a tool only when it materially helps answer the user.\n"
            "- Never invent required arguments.\n"
            "- If a required argument is missing, ask a clarifying question instead of calling the tool.\n"
            "- Use namespaced tools exactly as provided.\n"
            "- If a tool returns an error, explain the error briefly and ask only for the missing concrete data."
        ]
        tool_names = {str(getattr(tool, "name", "") or "").strip() for tool in tools}
        if "scheduler.create_task" in tool_names:
            lines.append(
                "[SCHEDULER_TOOL_HINTS]\n"
                "- For scheduler.create_task always send both schedule and steps.\n"
                "- schedule_type must match the schedule payload.\n"
                "- For schedule_type='interval', schedule must be like {'every': 10, 'unit': 'minutes'} or {'every': 2, 'unit': 'hours'}.\n"
                "- For schedule_type='once', schedule must be like {'run_at': '2026-03-12 18:30'}.\n"
                "- steps must be a non-empty list of namespaced tools.\n"
                "- Use save_result_as and arguments_template when later steps depend on earlier results."
            )
        return "\n".join(lines)

    def _augment_validation_error(self, tool_name: str, error_payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(tool_name or "").strip()
        if name != "scheduler.create_task":
            return error_payload
        message = str(error_payload.get("message") or "").strip()
        hint = (
            " scheduler.create_task requires schedule_type, schedule and steps. "
            "Example interval schedule: {'every':10,'unit':'minutes'}. "
            "Example once schedule: {'run_at':'2026-03-12 18:30'}. "
            "steps must be a non-empty list like [{'tool':'gismeteo.get_current_weather','save_result_as':'weather'}]."
        )
        error_payload = dict(error_payload)
        error_payload["message"] = message + hint
        return error_payload

    def _validate_tool_arguments(self, tool_name: str, tool_args: Dict[str, Any], tools: List[Any]) -> Optional[Dict[str, Any]]:
        name = str(tool_name or "").strip()
        match = next((tool for tool in tools if str(getattr(tool, "name", "") or "") == name), None)
        if match is None:
            return {
                "is_error": True,
                "error_type": "unknown_tool",
                "message": f"Tool '{name}' is not available in current MCP tools list.",
                "tool_name": name,
                "arguments": tool_args,
            }

        schema = match.input_schema if isinstance(match.input_schema, dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        missing = [key for key in required if str(tool_args.get(key) or "").strip() == ""]
        if missing:
            return self._augment_validation_error(name, {
                "is_error": True,
                "error_type": "missing_required_arguments",
                "message": f"Missing required tool arguments: {', '.join(missing)}.",
                "tool_name": name,
                "arguments": tool_args,
                "required": required,
            })

        placeholders = {
            "owner", "repo", "username", "organization", "org", "branch", "tag", "query", "path", "sha",
            "owner_name", "repo_name", "your_repo", "your_owner", "example", "value",
        }
        bad_fields: List[str] = []
        for key, value in tool_args.items():
            if not isinstance(value, str):
                continue
            clean = value.strip().lower()
            if not clean:
                continue
            if clean in placeholders or clean == key.strip().lower():
                bad_fields.append(key)
                continue
            if clean.startswith("your_") or clean.endswith("_name"):
                bad_fields.append(key)
        if bad_fields:
            descriptions = []
            for field in bad_fields:
                desc = ""
                if isinstance(properties.get(field), dict):
                    desc = str(properties[field].get("description") or "").strip()
                descriptions.append(f"{field}{f' ({desc})' if desc else ''}")
            return {
                "is_error": True,
                "error_type": "placeholder_arguments",
                "message": f"Tool arguments look like placeholders or invented values: {', '.join(descriptions)}.",
                "tool_name": name,
                "arguments": tool_args,
                "required": required,
            }
        return None

    async def _get_mcp_tools(self) -> Tuple[List[Any], Dict[str, Any]]:
        try:
            tools = await self.mcp.list_tools()
            return tools, {"enabled": True, "connected": True, "tools_count": len(tools)}
        except Exception as e:
            return [], {"enabled": self.mcp.enabled, "connected": False, "error": str(e), "tools_count": 0}

    def _extract_tool_calls(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        result: List[Dict[str, Any]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            result.append(
                {
                    "id": str(item.get("id") or str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or "").strip(),
                        "arguments": str(function.get("arguments") or "{}"),
                    },
                }
            )
        return result

    async def _handle_ping(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "pong"})

    async def _handle_list_sessions(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        sessions = [
            {
                "session_id": item.session_id,
                "title": item.title,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in self.memory_store.list_sessions()
        ]
        await self._send_json(writer, {"type": "sessions", "sessions": sessions})

    async def _handle_get_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        await self._send_json_maybe_chunked(writer, {"type": "session", "session": session})

    async def _handle_reset_session(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        ok = self.memory_store.delete_session_file(session_id)
        await self._send_json(writer, {"type": "ok" if ok else "error", "message": "" if ok else "session not found"})

    async def _handle_list_branches(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        branches = session.get("branches") if isinstance(session.get("branches"), dict) else {}
        items = []
        for bid, branch in branches.items():
            if not isinstance(branch, dict):
                continue
            items.append(
                {
                    "branch_id": bid,
                    "title": str(branch.get("title") or bid),
                    "created_at": str(branch.get("created_at") or ""),
                    "updated_at": str(branch.get("updated_at") or ""),
                }
            )
        await self._send_json(writer, {"type": "branches", "branches": items, "active_branch": session.get("active_branch") or "main"})

    async def _handle_switch_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        branch_id = str(request.get("branch_id") or "").strip()
        if not session_id or not branch_id:
            await self._send_error(writer, "session_id and branch_id are required")
            return
        session = self.memory_store.load_session(session_id)
        bid, _ = self._ensure_branch(session, branch_id)
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "ok", "active_branch": bid})

    async def _handle_list_checkpoints(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        branch_id = str(request.get("branch_id") or "").strip() or None
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        _, branch = self._ensure_branch(session, branch_id)
        checkpoints = branch.get("checkpoints") if isinstance(branch.get("checkpoints"), list) else []
        await self._send_json(writer, {"type": "checkpoints", "checkpoints": checkpoints, "active_branch": session.get("active_branch")})

    async def _handle_create_checkpoint(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        _, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        checkpoint = self._make_checkpoint(branch.get("history") if isinstance(branch.get("history"), list) else [], str(request.get("name") or ""))
        branch.setdefault("checkpoints", []).append(checkpoint)
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "checkpoint_created", "checkpoint": checkpoint})

    async def _handle_create_branch(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        from_branch_id = str(request.get("from_branch_id") or "").strip()
        checkpoint_id = str(request.get("checkpoint_id") or "").strip()
        if not session_id or not from_branch_id or not checkpoint_id:
            await self._send_error(writer, "session_id, from_branch_id and checkpoint_id are required")
            return
        session = self.memory_store.load_session(session_id)
        try:
            new_bid = self._create_branch_from_checkpoint(session, from_branch_id, checkpoint_id, str(request.get("new_branch_name") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "branch_created", "branch_id": new_bid})

    async def _handle_get_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            await self._send_error(writer, "session_id is required")
            return
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        await self._send_json(
            writer,
            {
                "type": "memory",
                "active_branch": active_branch,
                "memory_layers": branch.get("memory_layers") or {},
                "facts": branch.get("facts") or {},
            },
        )

    async def _handle_save_memory(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        session_id = str(request.get("session_id") or "").strip()
        layer = str(request.get("layer") or "").strip()
        value = str(request.get("value") or "").strip()
        key = str(request.get("key") or "").strip()
        if not session_id or not layer or not value:
            await self._send_error(writer, "session_id, layer and value are required")
            return
        session = self.memory_store.load_session(session_id)
        active_branch, branch = self._ensure_branch(session, str(request.get("branch_id") or "").strip() or None)
        memory_layers = self._copy_memory_layers(branch.get("memory_layers"))
        self._save_memory_item(memory_layers, layer, key, value)
        branch["memory_layers"] = memory_layers
        self.memory_store.save_session(session)
        await self._send_json(writer, {"type": "ok", "active_branch": active_branch, "memory_layers": memory_layers})

    async def _handle_get_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        profile_name = str(request.get("profile_name") or "").strip()
        profile = self.profile_store.get_profile(profile_name)
        if not isinstance(profile, dict):
            await self._send_error(writer, "profile not found")
            return
        await self._send_json(writer, {"type": "profile", "profile": profile})

    async def _handle_save_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            saved = self.profile_store.save_profile(str(request.get("profile_name") or ""), str(request.get("description") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_delete_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        saved = self.profile_store.delete_profile(str(request.get("profile_name") or ""))
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_set_active_profile(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        saved = self.profile_store.set_active_profile(str(request.get("profile_name") or ""))
        await self._send_json(
            writer,
            {
                "type": "ok",
                "profiles": sorted((saved.get("profiles") or {}).keys()),
                "active_profile": saved.get("active_profile") or "",
            },
        )

    async def _handle_get_profile_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_state()
        await self._send_json(
            writer,
            {
                "type": "profile_state",
                "profiles": state.get("available_profiles") or [],
                "active_profile": state.get("active_profile") or "",
            },
        )

    async def _handle_get_invariants_state(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        state = self.profile_store.get_invariants_state()
        await self._send_json(
            writer,
            {
                "type": "invariants_state",
                "invariants": state.get("invariants") or {},
                "invariant_policy": state.get("invariant_policy") or {},
            },
        )

    async def _handle_save_invariant(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            state = self.profile_store.save_invariant_value(str(request.get("key") or ""), str(request.get("value") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(writer, {"type": "ok", **state})

    async def _handle_set_invariant_policy(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            state = self.profile_store.set_invariant_policy(str(request.get("key") or ""), str(request.get("policy") or ""))
        except Exception as e:
            await self._send_error(writer, str(e))
            return
        await self._send_json(writer, {"type": "ok", **state})

    async def _handle_mcp_status(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await self._send_json(writer, {"type": "mcp_status", "status": await self.mcp.status()})

    async def _handle_mcp_list_tools(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            tools = await self.mcp.list_tools()
            await self._send_json(writer, {"type": "mcp_tools", "connected": True, "tools": self._serialize_mcp_tools(tools)})
        except Exception as e:
            await self._send_json(writer, {"type": "mcp_tools", "connected": False, "tools": [], "error": str(e)})

    async def _handle_mcp_call_tool(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        tool_name = str(request.get("tool_name") or request.get("name") or "").strip()
        arguments = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
        try:
            result = await self.mcp.call_tool(tool_name, arguments)
            await self._send_json(writer, {"type": "mcp_tool_result", "ok": True, "tool_name": tool_name, "result": result})
        except Exception as e:
            await self._send_json(writer, {"type": "mcp_tool_result", "ok": False, "tool_name": tool_name, "error": str(e)})

    async def _handle_stream_chat(self, request: Dict[str, Any], writer: asyncio.StreamWriter) -> None:
        req_id = str(uuid.uuid4())
        session_id = str(request.get("session_id") or "").strip() or str(uuid.uuid4())
        model = str(request.get("model") or self.gpt.model).strip()
        max_tokens = int(request.get("max_tokens") or 800)
        temperature = request.get("temperature")
        keep_last_n = int(request.get("keep_last_n") or 10)
        strategy = str(request.get("context_strategy") or "sliding").strip().lower()
        user_text = str(request.get("user_text") or "").strip()
        use_profile = bool(request.get("use_profile", False))
        if not user_text:
            await self._send_error(writer, "user_text is required")
            return

        session = self.memory_store.load_session(session_id)
        self.memory_store.set_title_if_empty(session, user_text)
        branch_key = str(request.get("branch_id") or "").strip() or None
        active_branch, branch = self._ensure_branch(session, branch_key if strategy == "branching" else "main")
        history = branch.get("history") if isinstance(branch.get("history"), list) else []
        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        memory_layers = self._copy_memory_layers(branch.get("memory_layers"))

        explicit_memory = request.get("memory_write") if isinstance(request.get("memory_write"), dict) else None
        if isinstance(explicit_memory, dict):
            self._save_memory_item(
                memory_layers,
                str(explicit_memory.get("layer") or ""),
                str(explicit_memory.get("key") or ""),
                str(explicit_memory.get("value") or ""),
            )
            branch["memory_layers"] = memory_layers

        user_text_for_api = user_text
        system_text = None
        if strategy == "facts":
            facts, cleaned_user_text = parse_facts_and_strip_user_text(user_text=user_text, prev_facts=facts)
            branch["facts"] = facts
            user_text_for_api = cleaned_user_text or "Учти обновленные факты и продолжай."
            facts_system_text, history_for_llm = build_facts_strategy(history, facts, keep_last_n)
            system_text = facts_system_text
        elif strategy == "summary":
            system_text, history_for_llm, summary_text = build_summary_strategy(history, keep_last_n, previous_summary=str(branch.get("summary") or ""))
            branch["summary"] = summary_text
        else:
            history_for_llm = build_sliding_window(history, keep_last_n)

        memory_system_text = self._build_memory_system_text(memory_layers)
        profile_state = self.profile_store.get_state()
        active_profile = str(profile_state.get("active_profile") or "").strip()
        profile_description = ""
        if active_profile:
            profile = self.profile_store.get_profile(active_profile)
            if isinstance(profile, dict):
                profile_description = str(profile.get("description") or "")
        profile_system_text = self._build_profile_system_text(active_profile, profile_description) if use_profile else None
        invariants_state = normalize_invariants_state(self.profile_store.get_invariants_state())
        invariants_text = build_invariants_system_text(
            invariants_state.get("invariants") or {},
            invariants_state.get("invariant_policy") or {},
        )
        mcp_tools, mcp_info = await self._get_mcp_tools()
        tools_policy_text = self._build_tool_usage_instruction(mcp_tools)
        system_text = self._merge_system_text(memory_system_text, profile_system_text, system_text, invariants_text, tools_policy_text)
        working_messages = self._build_messages_for_llm(system_text=system_text, history_for_llm=history_for_llm, user_text=user_text_for_api)
        self._log(
            "INFO",
            "CHAT_CONTEXT",
            {
                "req_id": req_id,
                "session_id": session_id,
                "branch_id": active_branch,
                "strategy": strategy,
                "system_text": system_text or "",
                "messages": working_messages,
            },
        )

        invariants_check = await self._validate_invariants(
            req_id=req_id,
            messages=working_messages,
            invariants_text=invariants_text,
            model=model,
        )
        self._log("INFO", "INVARIANTS_VALIDATION", invariants_check)

        openai_tools = [tool.to_openai_tool() for tool in mcp_tools]
        self._log("INFO", "MCP_TOOLS_FOR_LLM", {"req_id": req_id, "mcp_info": mcp_info, "tools": openai_tools})

        usage_agg: Dict[str, Any] = {}
        final_answer = ""
        tool_events: List[Dict[str, Any]] = []
        loop_guard = 0
        max_tool_iterations = max(8, int(str(os.getenv("AGENT_MAX_TOOL_ITERATIONS", "32")).strip() or "32"))
        history.append({"role": "user", "content": user_text})

        while loop_guard < max_tool_iterations:
            loop_guard += 1
            llm_result = await self.gpt.chat_completion(
                messages=working_messages,
                model=model,
                max_tokens=max_tokens,
                temperature=float(temperature) if temperature is not None else None,
                tools=openai_tools if openai_tools else None,
                tool_choice="auto",
                trace_id=f"{req_id}:loop:{loop_guard}",
            )
            usage_agg = self._merge_usage(usage_agg, llm_result.get("usage") if isinstance(llm_result.get("usage"), dict) else {})
            assistant_message = llm_result.get("message") if isinstance(llm_result.get("message"), dict) else {}
            tool_calls = self._extract_tool_calls(assistant_message)
            self._log("INFO", "LLM_ASSISTANT_MESSAGE", assistant_message)

            if tool_calls:
                assistant_entry = {"role": "assistant", "content": assistant_message.get("content"), "tool_calls": tool_calls}
                working_messages.append(assistant_entry)
                history.append(assistant_entry)
                for tool_call in tool_calls:
                    tool_name = str((tool_call.get("function") or {}).get("name") or "").strip()
                    raw_args = str((tool_call.get("function") or {}).get("arguments") or "{}")
                    try:
                        tool_args = json.loads(raw_args) if raw_args.strip() else {}
                        if not isinstance(tool_args, dict):
                            tool_args = {"value": tool_args}
                    except Exception as e:
                        tool_args = {"_raw_arguments": raw_args, "_parse_error": str(e)}
                    validation_error = self._validate_tool_arguments(tool_name, tool_args, mcp_tools)
                    try:
                        if validation_error is not None:
                            raise MCPClientError(validation_error.get("message") or "Invalid tool arguments")
                        tool_result = await self.mcp.call_tool(tool_name, tool_args)
                    except MCPClientError as e:
                        if validation_error is not None:
                            tool_result = dict(validation_error)
                        else:
                            tool_result = {"is_error": True, "error": str(e), "tool_name": tool_name, "arguments": tool_args}
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": self._normalize_tool_result_for_history(tool_result),
                    }
                    working_messages.append(tool_message)
                    history.append(tool_message)
                    event = {"tool_call": tool_call, "tool_result": tool_result}
                    tool_events.append(event)
                    self._log("INFO", "LLM_TOOL_ROUNDTRIP", event)
                continue

            final_answer = str(assistant_message.get("content") or "")
            history.append({"role": "assistant", "content": final_answer})
            break

        if not final_answer:
            final_answer = "LLM не вернула финальный текстовый ответ."

        self._sync_short_term_from_history(memory_layers, history)
        branch["history"] = history
        branch["memory_layers"] = memory_layers
        branch["facts"] = facts
        branch["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["updated_at"] = branch["updated_at"]
        session["active_branch"] = active_branch
        self.memory_store.save_session(session)

        if final_answer:
            await self._send_json(writer, {"type": "chunk", "chunk": final_answer})

        token_stats = {
            "user_text_tokens_est": self._estimate_tokens_text(user_text_for_api),
            "context_tokens_est": self._estimate_tokens_messages(working_messages[:-1]) if working_messages else 0,
            "assistant_tokens": int(usage_agg.get("completion_tokens") or usage_agg.get("output_tokens") or self._estimate_tokens_text(final_answer)),
            "total_tokens_call": int(usage_agg.get("total_tokens") or 0),
            "dialog_tokens_est": self._estimate_tokens_messages(history),
            "model_context_limit": self._resolve_context_limit(model),
            "may_exceed_context": False,
        }
        token_stats["may_exceed_context"] = bool(
            token_stats["context_tokens_est"] + token_stats["user_text_tokens_est"] > token_stats["model_context_limit"]
        )
        message_stats = {
            "strategy": strategy,
            "branch_id": active_branch,
            "use_profile": use_profile,
            "active_profile": active_profile,
            "profile_description_len": len(profile_description),
            "profile_applied": bool(profile_system_text),
            "keep_last_n": keep_last_n,
            "sent_messages": len(working_messages),
            "facts_count": len(facts) if isinstance(facts, dict) else 0,
            "memory_layers_counts": {
                "short_term": len(memory_layers.get("short_term") or []),
                "working": len(memory_layers.get("working") or {}),
                "long_term": len(memory_layers.get("long_term") or {}),
            },
            "invariants_decision": invariants_check.get("decision") or "pass",
            "invariants_reason": invariants_check.get("reason") or "",
            "tool_iterations": loop_guard,
            "tools_available": len(mcp_tools),
        }
        cost_rub = self._calc_cost_rub(model_id=model, usage=usage_agg)
        done_payload = {
            "type": "done",
            "model": model,
            "endpoint": "chat",
            "usage": usage_agg,
            "cost_rub": cost_rub,
            "session_id": session_id,
            "title": session.get("title") or "",
            "active_branch": active_branch,
            "message_stats": message_stats,
            "facts": facts if isinstance(facts, dict) else {},
            "memory_layers": memory_layers,
            "token_stats": token_stats,
            "profile_info": {
                "use_profile": use_profile,
                "active_profile": active_profile,
                "profile_description_len": len(profile_description),
                "profile_applied": bool(profile_system_text),
            },
            "mcp_info": {**mcp_info, "tools": self._serialize_mcp_tools(mcp_tools)},
            "tool_events": tool_events,
        }
        self._log("INFO", "CHAT_DONE", done_payload)
        await self._send_json(writer, done_payload)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self.logger.write("INFO", "Клиент подключился", extra=str(peer))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    await self._send_json(writer, {"type": "error", "message": "Invalid JSON"})
                    continue
                action = str(request.get("action") or "").strip()
                handler = self._action_handlers.get(action)
                if handler is None:
                    await self._send_error(writer, "Unknown action")
                    continue
                await handler(request, writer)
        except Exception as e:
            self.logger.write("ERROR", "handle_client", extra=str(e))
            self.logger.write("ERROR", "TRACEBACK", extra=traceback.format_exc())
            try:
                await self._send_json(writer, {"type": "error", "message": str(e) or "Unknown error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.logger.write("INFO", "Клиент отключился", extra=str(peer))

    async def run(self) -> None:
        await self.preload_pricing()
        await self._ensure_scheduler_worker()
        status = await self.mcp.status()
        self._log("INFO", "MCP_STATUS_AT_STARTUP", status)
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        self.logger.write("INFO", "Агент запущен и слушает", extra=addrs)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self._stop_scheduler_worker()


async def main() -> None:
    agent = LLMAgentServer()
    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


