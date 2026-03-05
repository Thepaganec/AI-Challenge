import re
from typing import Any, Dict, List, Tuple


INVARIANT_KEYS: Tuple[str, ...] = (
    "architecture",
    "technical_decisions",
    "stack_constraints",
    "business_rules",
    "safety_restrictions",
    "response_style",
)
INVARIANT_POLICIES: Tuple[str, ...] = ("strict", "warn")
KNOWN_TECH_TERMS: Tuple[str, ...] = (
    "java",
    "kotlin",
    "python",
    "javascript",
    "typescript",
    "c#",
    "c++",
    "go",
    "golang",
    "rust",
    "php",
    "ruby",
    "scala",
    "swift",
    "sql",
    "mysql",
    "postgres",
    "postgresql",
    "oracle",
    "sqlite",
    "mssql",
    "mongodb",
    "mongo",
    "redis",
)


def normalize_invariants_state(raw: Any) -> Dict[str, Dict[str, str]]:
    invariants: Dict[str, str] = {k: "" for k in INVARIANT_KEYS}
    policy: Dict[str, str] = {k: "strict" for k in INVARIANT_KEYS}
    if isinstance(raw, dict):
        source_invariants = raw.get("invariants") if isinstance(raw.get("invariants"), dict) else {}
        source_policy = raw.get("invariant_policy") if isinstance(raw.get("invariant_policy"), dict) else {}
        for k in INVARIANT_KEYS:
            invariants[k] = str(source_invariants.get(k) or "").strip()
            mode = str(source_policy.get(k) or "strict").strip().lower()
            if mode in INVARIANT_POLICIES:
                policy[k] = mode
    return {"invariants": invariants, "invariant_policy": policy}


def build_runtime_context_text(stage: str) -> str:
    clean_stage = str(stage or "chat").strip() or "chat"
    return f"[RUNTIME_CONTEXT]\nstage: {clean_stage}"


def build_invariants_system_text(invariants: Dict[str, str], policy: Dict[str, str]) -> str:
    lines: List[str] = []
    for key in INVARIANT_KEYS:
        value = str(invariants.get(key) or "").strip()
        if not value:
            continue
        mode = str(policy.get(key) or "strict").strip().lower()
        if mode not in INVARIANT_POLICIES:
            mode = "strict"
        lines.append(f"- {key} [{mode}]: {value}")
    if not lines:
        return ""
    return (
        "[INVARIANTS]\n"
        "Treat strict invariants as hard constraints.\n"
        "If warn invariant is at risk, include explicit warning.\n"
        + "\n".join(lines)
    )


def _split_terms(raw: str) -> List[str]:
    data = str(raw or "").strip()
    if not data:
        return []
    parts = re.split(r"(?:[\n,;]+|\s+или\s+|\s+or\s+|/)", data, flags=re.IGNORECASE)
    out: List[str] = []
    for part in parts:
        token = " ".join(str(part).strip().split()).strip(" .,:;\"'`()[]{}")
        if token and token not in out:
            out.append(token)
    return out


def _parse_invariant_value(raw_value: str) -> Dict[str, List[str]]:
    value = str(raw_value or "").strip()
    if not value:
        return {"forbid": [], "warn": [], "allow_only": []}
    forbid: List[str] = []
    warn: List[str] = []
    allow_only: List[str] = []

    # Machine-readable directives.
    for line in value.splitlines():
        clean = str(line or "").strip()
        if not clean:
            continue
        low = clean.lower()
        if low.startswith("forbid:") or low.startswith("deny:"):
            forbid.extend(_split_terms(clean.split(":", 1)[1]))
        elif low.startswith("allow_only:") or low.startswith("only:"):
            allow_only.extend(_split_terms(clean.split(":", 1)[1]))
        elif low.startswith("warn:"):
            warn.extend(_split_terms(clean.split(":", 1)[1]))

    # Natural-language constraints: "не используй X", "нельзя использовать X".
    for pat in (
        r"(?:не\s+используй(?:те)?|нельзя\s+использовать|запрещено\s+использовать|не\s+применяй(?:те)?)\s+([^\n\.;]+)",
    ):
        for m in re.finditer(pat, value, flags=re.IGNORECASE):
            forbid.extend(_split_terms(m.group(1)))

    # Natural-language allow-list: "используй только Kotlin или Java".
    for pat in (
        r"(?:всегда\s+)?(?:используй(?:те)?|применяй(?:те)?|use)\s+только\s+([^\n\.;]+)",
        r"only\s+use\s+([^\n\.;]+)",
    ):
        for m in re.finditer(pat, value, flags=re.IGNORECASE):
            allow_only.extend(_split_terms(m.group(1)))

    return {
        "forbid": list(dict.fromkeys(forbid)),
        "warn": list(dict.fromkeys(warn)),
        "allow_only": list(dict.fromkeys(allow_only)),
    }


def _contains_term(text: str, term: str) -> bool:
    hay = str(text or "")
    needle = str(term or "").strip()
    if not needle:
        return False
    # Word-like terms match by boundaries; symbols fallback to plain substring.
    if re.match(r"^[A-Za-zА-Яа-я0-9_+#.\-]+$", needle):
        pattern = rf"(?<![A-Za-zА-Яа-я0-9_]){re.escape(needle)}(?![A-Za-zА-Яа-я0-9_])"
        return re.search(pattern, hay, flags=re.IGNORECASE) is not None
    return needle.lower() in hay.lower()


def _detect_tech_terms(text: str) -> List[str]:
    found: List[str] = []
    for term in KNOWN_TECH_TERMS:
        if _contains_term(text, term):
            found.append(term.lower())
    return list(dict.fromkeys(found))


def validate_text_with_invariants(
    text: str,
    invariants: Dict[str, str],
    policy: Dict[str, str],
) -> Dict[str, Any]:
    candidate = str(text or "")
    warnings: List[str] = []
    for key in INVARIANT_KEYS:
        value = str(invariants.get(key) or "").strip()
        if not value:
            continue
        mode = str(policy.get(key) or "strict").strip().lower()
        if mode not in INVARIANT_POLICIES:
            mode = "strict"
        parsed = _parse_invariant_value(value)
        for term in parsed["forbid"]:
            if _contains_term(candidate, term):
                reason = f"Нарушен инвариант `{key}`: найден запрещенный термин `{term}`."
                if mode == "strict":
                    return {
                        "decision": "fail",
                        "conflict_key": key,
                        "reason": reason,
                        "warnings": warnings,
                    }
                warnings.append(reason)
        for term in parsed["warn"]:
            if _contains_term(candidate, term):
                warnings.append(f"Риск по инварианту `{key}`: `{term}`.")
        allowed = [str(x).strip().lower() for x in parsed.get("allow_only", []) if str(x).strip()]
        if allowed:
            detected = _detect_tech_terms(candidate)
            forbidden_detected = [x for x in detected if x not in allowed]
            if forbidden_detected:
                reason = (
                    f"Нарушен инвариант `{key}`: обнаружены недопустимые технологии "
                    f"{', '.join(forbidden_detected)}; разрешено только {', '.join(allowed)}."
                )
                if mode == "strict":
                    return {
                        "decision": "fail",
                        "conflict_key": key,
                        "reason": reason,
                        "warnings": warnings,
                    }
                warnings.append(reason)
    if warnings:
        return {"decision": "warn", "conflict_key": "", "reason": "", "warnings": warnings}
    return {"decision": "pass", "conflict_key": "", "reason": "", "warnings": []}
