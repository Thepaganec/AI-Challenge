from typing import Any, Dict, List

from core.shared.schema_validation import validate_json_value


SCHEDULER_STEP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "Public orchestrator tool name such as telegram__send_message"},
        "arguments": {"type": "object", "description": "Static arguments for the tool call", "additionalProperties": True},
        "arguments_template": {
            "type": "object",
            "description": "Template arguments rendered from execution memory with placeholders like {{weather.summary}}",
            "additionalProperties": True,
        },
        "save_result_as": {"type": "string", "description": "Execution memory key for storing the full tool result"},
    },
    "required": ["tool"],
    "additionalProperties": False,
}

SCHEDULER_INTERVAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "every": {"type": "integer"},
        "unit": {"type": "string", "enum": ["minutes", "hours"]},
    },
    "required": ["every", "unit"],
    "additionalProperties": False,
}

SCHEDULER_ONCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_at": {"type": "string"},
    },
    "required": ["run_at"],
    "additionalProperties": False,
}

SCHEDULER_TIME_POINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "hour": {"type": "integer"},
        "minute": {"type": "integer"},
    },
    "required": ["hour", "minute"],
    "additionalProperties": False,
}

SCHEDULER_DAILY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "time_points": {
            "type": "array",
            "items": SCHEDULER_TIME_POINT_SCHEMA,
            "minItems": 1,
        }
    },
    "required": ["time_points"],
    "additionalProperties": False,
}

SCHEDULER_WEEKLY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ],
            },
            "minItems": 1,
        },
        "time_points": {
            "type": "array",
            "items": SCHEDULER_TIME_POINT_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["days", "time_points"],
    "additionalProperties": False,
}

SCHEDULER_CREATE_TASK_ALLOWED_FIELDS = {
    "title",
    "schedule_type",
    "schedule",
    "steps",
    "template_text",
    "metadata",
    "trace_id",
}


def scheduler_create_task_input_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "schedule_type": {"type": "string", "enum": ["once", "interval", "daily", "weekly"]},
            "schedule": {
                "type": "object",
                "description": "Schedule object. interval => {every, unit}; once => {run_at}; daily => {time_points}; weekly => {days, time_points}.",
                "additionalProperties": False,
                "properties": {
                    "every": {"type": "integer"},
                    "unit": {"type": "string", "enum": ["minutes", "hours"]},
                    "run_at": {"type": "string"},
                    "days": {"type": "array", "items": {"type": "string"}},
                    "time_points": {"type": "array", "items": SCHEDULER_TIME_POINT_SCHEMA},
                },
            },
            "steps": {
                "type": "array",
                "items": SCHEDULER_STEP_SCHEMA,
                "minItems": 1,
            },
            "template_text": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
            "trace_id": {"type": "string"},
        },
        "required": ["title", "schedule_type", "schedule", "steps"],
        "additionalProperties": False,
    }


def scheduler_contract_examples() -> Dict[str, Any]:
    return {
        "interval": {
            "title": "Погода в Telegram каждые 10 минут",
            "schedule_type": "interval",
            "schedule": {"every": 10, "unit": "minutes"},
            "steps": [
                {
                    "tool": "gismeteo__get_current_weather",
                    "save_result_as": "weather",
                },
                {
                    "tool": "telegram__send_message",
                    "arguments_template": {
                        "chat_id": "555964088",
                        "text": "Погода сейчас: {{weather.summary}}",
                    },
                },
            ],
        },
        "once": {
            "title": "Разовая отправка погоды",
            "schedule_type": "once",
            "schedule": {"run_at": "2026-03-15 21:30"},
            "steps": [
                {"tool": "gismeteo__get_current_weather", "save_result_as": "weather"},
                {
                    "tool": "telegram__send_message",
                    "arguments_template": {
                        "chat_id": "555964088",
                        "text": "Разовая отправка: {{weather.summary}}",
                    },
                },
            ],
        },
    }


def _contains_single_braces(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_single_braces(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_single_braces(item) for item in value)
    if not isinstance(value, str):
        return False
    return ("{" in value or "}" in value) and "{{" not in value and "}}" not in value


def validate_scheduler_create_task_payload(payload: Dict[str, Any]) -> List[str]:
    if not isinstance(payload, dict):
        return ["$: scheduler payload must be an object"]

    errors: List[str] = []
    for key in payload.keys():
        if key not in SCHEDULER_CREATE_TASK_ALLOWED_FIELDS:
            errors.append(f"$: unexpected field '{key}'")

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("$.title: expected non-empty string")

    schedule_type = str(payload.get("schedule_type") or "").strip().lower()
    if schedule_type not in {"once", "interval", "daily", "weekly"}:
        errors.append("$.schedule_type: expected one of once, interval, daily, weekly")

    schedule = payload.get("schedule")
    schedule_schema = {
        "once": SCHEDULER_ONCE_SCHEMA,
        "interval": SCHEDULER_INTERVAL_SCHEMA,
        "daily": SCHEDULER_DAILY_SCHEMA,
        "weekly": SCHEDULER_WEEKLY_SCHEMA,
    }.get(schedule_type)
    if schedule_schema is None:
        if "schedule" not in payload:
            errors.append("$: missing required field 'schedule'")
    else:
        errors.extend(validate_json_value(schedule, schedule_schema, path="$.schedule"))

    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("$.steps: expected non-empty list")
        return errors

    for index, step in enumerate(steps):
        step_path = f"$.steps[{index}]"
        errors.extend(validate_json_value(step, SCHEDULER_STEP_SCHEMA, path=step_path))
        if not isinstance(step, dict):
            continue
        tool_name = str(step.get("tool") or "").strip()
        if tool_name.startswith("functions."):
            errors.append(f"{step_path}.tool: use public MCP tool name without 'functions.' prefix")
        if "." in tool_name and "__" not in tool_name:
            errors.append(f"{step_path}.tool: use public MCP tool name like 'telegram__send_message'")
        has_arguments = isinstance(step.get("arguments"), dict) and bool(step.get("arguments"))
        has_template = isinstance(step.get("arguments_template"), dict) and bool(step.get("arguments_template"))
        if has_arguments and has_template:
            errors.append(f"{step_path}: use either 'arguments' or 'arguments_template', not both")
        if has_template and _contains_single_braces(step.get("arguments_template")):
            errors.append(f"{step_path}.arguments_template: placeholders must use '{{{{memory.path}}}}' syntax")

    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append("$.metadata: expected object")

    template_text = payload.get("template_text")
    if template_text is not None and not isinstance(template_text, str):
        errors.append("$.template_text: expected string")
    if isinstance(template_text, str) and template_text.strip():
        errors.append("$.template_text: unsupported field for scheduler routing, use steps[*].arguments_template instead")

    return errors
