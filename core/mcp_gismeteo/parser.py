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
            try:
                value = match.group(1)
            except IndexError:
                value = match.group(0)
            return _strip_tags(value)
    return ""


def parse_current_weather(html: str, source_url: str) -> Dict[str, Any]:
    page_text = _strip_tags(html)
    title = _search([r"<title[^>]*>(.*?)</title>"], html)
    city = _search([r"Погода в\s+([^,]+)\s+сегодня"], title) or "Твери"
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
    wind = _search([r"Ветер[^0-9]*([0-9.,]+\s*(?:м/с|m/s))"], page_text)
    humidity = _search([r"Влажность[^0-9]*([0-9]{1,3}\s*%)"], page_text)
    pressure = _search([r"Давление[^0-9]*([0-9]{2,4}\s*(?:мм|hPa)[^ ]*)"], page_text)

    condition = "Актуальный прогноз"
    summary_parts = []
    if city:
        summary_parts.append(f"Погода в {city} сейчас")
    if temperature:
        summary_parts.append(f"температура {temperature} C")
    if feels_like:
        summary_parts.append(f"ощущается как {feels_like} C")
    if wind:
        summary_parts.append(f"ветер {wind}")
    if humidity:
        summary_parts.append(f"влажность {humidity}")
    summary = ", ".join(summary_parts).strip() or title or "Актуальный прогноз"

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
        "city": city,
        "raw_text_excerpt": page_text[:1200],
    }
