import re
from html import unescape
from typing import Any, Dict, Optional


def _strip_tags(value: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", value)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _search(patterns: list[str], html: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return _strip_tags(match.group(1))
    return ""


def parse_current_weather(html: str, source_url: str) -> Dict[str, Any]:
    page_text = _strip_tags(html)
    title = _search([r"<title[^>]*>(.*?)</title>"], html)
    temperature = _search(
        [
            r'data-widget="weather-now".*?class="unit unit_temperature_c".*?([-+]?\d+)',
            r'class="temperature-air[^"]*".*?([-+]?\d+)',
            r'([-+]?\d+)\s*(?:°|&deg;|C)',
        ],
        html,
    )
    feels_like = _search(
        [
            r"По ощущению[^-+0-9]*([-+]?\d+)",
            r"Ощущается как[^-+0-9]*([-+]?\d+)",
        ],
        page_text,
    )
    condition = _search(
        [
            r'class="weather-description[^"]*">(.*?)</',
            r"Сейчас[^.]{0,120}",
        ],
        html,
    ) or "Актуальный прогноз получен"
    wind = _search([r"Ветер[^0-9]*([0-9.,]+\s*(?:м/с|m/s))"], page_text)
    humidity = _search([r"Влажность[^0-9]*([0-9]{1,3}\s*%)"], page_text)
    pressure = _search([r"Давление[^0-9]*([0-9]{2,4}\s*(?:мм|hPa)[^ ]*)"], page_text)

    summary_parts = [part for part in [condition, f"Температура {temperature}" if temperature else ""] if part]
    summary = ". ".join(summary_parts).strip(". ").strip() or title or "Актуальный прогноз"

    return {
        "ok": True,
        "source_url": source_url,
        "title": title,
        "summary": summary,
        "temperature": temperature,
        "feels_like": feels_like,
        "condition": condition,
        "wind": wind,
        "humidity": humidity,
        "pressure": pressure,
        "raw_text_excerpt": page_text[:1200],
    }
