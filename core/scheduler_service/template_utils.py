import re
from typing import Any, Dict


_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_\.]+)\s*\}\}")


def _lookup(memory: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = memory
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise KeyError(dotted_path)
    return current


def render_value(template: Any, memory: Dict[str, Any]) -> Any:
    if isinstance(template, dict):
        return {key: render_value(value, memory) for key, value in template.items()}
    if isinstance(template, list):
        return [render_value(item, memory) for item in template]
    if not isinstance(template, str):
        return template

    matches = list(_TEMPLATE_PATTERN.finditer(template))
    if not matches:
        return template
    if len(matches) == 1 and matches[0].span() == (0, len(template)):
        return _lookup(memory, matches[0].group(1))

    def _replace(match: re.Match[str]) -> str:
        value = _lookup(memory, match.group(1))
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, template)
