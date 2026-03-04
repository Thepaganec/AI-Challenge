import re
from typing import Dict, List, Optional, Set, Tuple


def build_sliding_window(history: List[dict], keep_last_n: int) -> List[dict]:
    """
    Sliding Window по ТЗ Day 10 считаем в *сообщениях*, но сохраняем *полные диалоговые итерации*.

    Правило: 1 отправка = 2 сообщения (user -> assistant).
    Поэтому:
      keep_last_n = 10  => 5 последних "поворотов" (5 user + 5 assistant)
    """
    if keep_last_n <= 0:
        return []

    # Считаем "повороты диалога" (turns): обычно это [user, assistant].
    turns: List[List[dict]] = []
    current: List[dict] = []

    for msg in history or []:
        role = (msg or {}).get("role")

        if role == "user":
            # если до этого остался незавершённый turn — сохраняем как есть
            if current:
                turns.append(current)
                current = []
            current.append(msg)
            continue

        if role == "assistant":
            if not current:
                # на всякий случай: assistant без user
                current = [msg]
                turns.append(current)
                current = []
            else:
                current.append(msg)
                turns.append(current)
                current = []
            continue

        # неизвестная роль — просто добавим в текущий turn
        if not current:
            current = [msg]
        else:
            current.append(msg)

    if current:
        turns.append(current)

    # keep_last_n — это количество сообщений (user+assistant). Один turn ~ 2 сообщения.
    max_turns = keep_last_n // 2
    if max_turns <= 0:
        return []

    last_turns = turns[-max_turns:]
    flattened: List[dict] = [m for t in last_turns for m in t]
    return flattened

def parse_facts_from_user_text(user_text: str, prev_facts: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    facts, _ = parse_facts_and_strip_user_text(user_text=user_text, prev_facts=prev_facts)
    return facts


def parse_facts_and_strip_user_text(user_text: str, prev_facts: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], str]:
    """
    Извлекает факты и возвращает (updated_facts, cleaned_user_text).
    В cleaned_user_text удаляются строки, которые распознаны как facts.

    Факты обновляются только по явным паттернам:
    - fact: key = value
    - факт: key = value
    - key: value, где key выглядит как одно из важных ключевых слов
    - отдельные фразы (моя цель/предпочитаю/ограничения/требование)
    """
    facts: Dict[str, str] = dict(prev_facts or {})

    text = (user_text or "").strip()
    if not text:
        return facts, ""

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    consumed_idx: Set[int] = set()

    # 1) явные fact/факт команды
    for i, ln in enumerate(lines):
        m = re.match(r"^(?:fact|факт)\s*:\s*(.+)$", ln, flags=re.I)
        if m:
            rest = m.group(1).strip()
            m2 = re.match(r"^([^=]+?)\s*=\s*(.+)$", rest)
            if m2:
                k = m2.group(1).strip()
                v = m2.group(2).strip()
                if k:
                    facts[k] = v
                    consumed_idx.add(i)
            continue

    # 2) key: value для ключей из "важных"
    important_keys = [
        "цель", "цели",
        "ограничение", "ограничения",
        "предпочтение", "предпочтения",
        "решение", "решения",
        "договоренность", "договоренности", "договорённость", "договорённости",
        "требование", "требования",
    ]
    for i, ln in enumerate(lines):
        m = re.match(r"^([^:]{2,40})\s*:\s*(.+)$", ln)
        if not m:
            continue
        k = m.group(1).strip()
        v = m.group(2).strip()
        if not k or not v:
            continue
        kl = k.lower()
        if any(ik in kl for ik in important_keys):
            facts[k] = v
            consumed_idx.add(i)

    # 3) фразы
    m = re.search(r"\bмоя\s+цель\s*[:\-]?\s*(.+)$", text, flags=re.I)
    if m:
        facts["Цель"] = m.group(1).strip()

    m = re.search(r"\bпредпочитаю\s*[:\-]?\s*(.+)$", text, flags=re.I)
    if m:
        facts["Предпочтения"] = m.group(1).strip()

    m = re.search(r"\bограничени[ея]\s*[:\-]?\s*(.+)$", text, flags=re.I)
    if m:
        facts["Ограничения"] = m.group(1).strip()

    m = re.search(r"\bтребовани[ея]\s*[:\-]?\s*(.+)$", text, flags=re.I)
    if m:
        facts["Требование"] = m.group(1).strip()

    # 4) помечаем строковые facts-фразы, чтобы не дублировать их в user-сообщении для API
    for i, ln in enumerate(lines):
        if re.search(r"\bмоя\s+цель\b", ln, flags=re.I):
            consumed_idx.add(i)
            continue
        if re.search(r"\bпредпочитаю\b", ln, flags=re.I):
            consumed_idx.add(i)
            continue
        if re.search(r"\bограничени[ея]\b", ln, flags=re.I):
            consumed_idx.add(i)
            continue
        if re.search(r"\bтребовани[ея]\b", ln, flags=re.I):
            consumed_idx.add(i)
            continue

    # 5) чистим пустые facts
    for k in list(facts.keys()):
        v = facts.get(k)
        if not k or v is None or str(v).strip() == "":
            facts.pop(k, None)

    cleaned_lines = [ln for idx, ln in enumerate(lines) if idx not in consumed_idx]
    cleaned_text = "\n".join(cleaned_lines).strip()
    return facts, cleaned_text


def facts_to_system_text(facts: Dict[str, str]) -> Optional[str]:
    if not isinstance(facts, dict) or not facts:
        return None

    lines = []
    for k, v in facts.items():
        if k and v is not None:
            lines.append(f"- {k}: {v}")
    if not lines:
        return None

    return "FACTS (важные факты/договорённости, держи в голове всегда):\n" + "\n".join(lines)


def build_facts_strategy(history: List[dict], facts: Dict[str, str], keep_last_n: int) -> Tuple[Optional[str], List[dict]]:
    system_text = facts_to_system_text(facts)
    tail = build_sliding_window(history, keep_last_n)
    return system_text, tail


def build_summary_strategy(
    history: List[dict],
    keep_last_n: int,
    previous_summary: str = "",
    max_summary_lines: int = 28,
    max_line_len: int = 220,
) -> Tuple[Optional[str], List[dict], str]:
    """
    Summary-compression strategy:
    - tail: последние N сообщений (как есть)
    - summary: сжатие более старой части диалога в краткий текст
    """
    if keep_last_n < 1:
        keep_last_n = 1

    safe_history = history or []
    if len(safe_history) <= keep_last_n:
        summary_text = (previous_summary or "").strip()
        system_text = f"SUMMARY OF PREVIOUS DIALOG:\n{summary_text}" if summary_text else None
        return system_text, list(safe_history), summary_text

    older = safe_history[:-keep_last_n]
    tail = safe_history[-keep_last_n:]

    new_lines: List[str] = []
    for msg in older:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower() or "unknown"
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        short = content.replace("\n", " ")
        short = re.sub(r"\s+", " ", short).strip()
        if len(short) > max_line_len:
            short = short[: max_line_len - 3].rstrip() + "..."
        new_lines.append(f"- {role}: {short}")

    merged: List[str] = []
    if previous_summary and previous_summary.strip():
        merged.extend([ln.strip() for ln in previous_summary.splitlines() if ln.strip()])
    merged.extend(new_lines)

    if len(merged) > max_summary_lines:
        merged = merged[-max_summary_lines:]

    summary_text = "\n".join(merged).strip()
    system_text = f"SUMMARY OF PREVIOUS DIALOG:\n{summary_text}" if summary_text else None
    return system_text, tail, summary_text
