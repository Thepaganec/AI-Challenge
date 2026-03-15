from typing import Any, Dict, List


def _allowed_types(schema: Dict[str, Any]) -> List[str]:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return [str(item).strip().lower() for item in schema_type if str(item).strip()]
    clean = str(schema_type or "").strip().lower()
    return [clean] if clean else []


def _matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    return True


def validate_json_value(value: Any, schema: Dict[str, Any], *, path: str = "$") -> List[str]:
    if not isinstance(schema, dict) or not schema:
        return []

    errors: List[str] = []
    allowed_types = _allowed_types(schema)
    if allowed_types and not any(_matches_type(value, schema_type) for schema_type in allowed_types):
        return [f"{path}: expected type {'|'.join(allowed_types)}, got {type(value).__name__}"]

    enum_values = schema.get("enum") if isinstance(schema.get("enum"), list) else None
    if enum_values is not None and value not in enum_values:
        return [f"{path}: expected one of {', '.join(map(str, enum_values))}, got {value!r}"]

    if value is None:
        return errors

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required field '{key}'")
        if schema.get("additionalProperties") is False:
            for key in value.keys():
                if key not in properties:
                    errors.append(f"{path}: unexpected field '{key}'")
        for key, item in value.items():
            child_schema = properties.get(key) if isinstance(properties.get(key), dict) else None
            if child_schema is None:
                continue
            errors.extend(validate_json_value(item, child_schema, path=f"{path}.{key}"))
        return errors

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: expected at least {min_items} item(s)")
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        for index, item in enumerate(value):
            errors.extend(validate_json_value(item, item_schema, path=f"{path}[{index}]"))
        return errors

    return errors
