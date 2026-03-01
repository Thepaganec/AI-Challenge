import re
from typing import Dict, List, Optional, Tuple


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
    """
    Простая (правда простая) эвристика обновления facts из текста пользователя.
    - Поддерживает явный формат:
        fact: key = value
        факт: key = value
        key: value  (если key выглядит как "цель/ограничения/предпочтения/решение/договоренность")
    - А также ловит распространённые фразы:
        "моя цель ..." / "цель: ..."
        "ограничение ..." / "ограничения: ..."
        "предпочитаю ..." / "предпочтения: ..."
    """
    facts: Dict[str, str] = dict(prev_facts or {})

    text = (user_text or "").strip()
    if not text:
        return facts

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lowered = text.lower()

    # 1) явные fact/факт команды
    for ln in lines:
        m = re.match(r"^(?:fact|факт)\s*:\s*(.+)$", ln, flags=re.I)
        if m:
            rest = m.group(1).strip()
            m2 = re.match(r"^([^=]+?)\s*=\s*(.+)$", rest)
            if m2:
                k = m2.group(1).strip()
                v = m2.group(2).strip()
                if k:
                    facts[k] = v
            continue

    # 2) key: value для ключей из "важных"
    important_keys = [
        "цель", "цели",
        "ограничение", "ограничения",
        "предпочтение", "предпочтения",
        "решение", "решения",
        "договоренность", "договоренности", "договорённость", "договорённости",
    ]
    for ln in lines:
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

    # немного чистим
    for k in list(facts.keys()):
        if facts[k] is None:
            facts.pop(k, None)
            continue
        v = str(facts[k]).strip()
        if not v:
            facts.pop(k, None)
            continue
        facts[k] = v

    return facts


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
