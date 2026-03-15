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
def build_invariants_state(raw: Any) -> Dict[str, Dict[str, str]]:
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
