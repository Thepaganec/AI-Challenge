import re
from html import unescape
from typing import Any, Dict


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


def _extract_widget_now_block(html: str) -> str:
    marker = 'data-widget="weather-now"'
    start = html.find(marker)
    if start < 0:
        return ""
    div_start = html.rfind("<div", 0, start)
    if div_start < 0:
        return ""

    pos = div_start
    depth = 0
    length = len(html)
    while pos < length:
        next_open = html.find("<div", pos)
        next_close = html.find("</div>", pos)
        if next_close < 0:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 4
            continue
        depth -= 1
        pos = next_close + len("</div>")
        if depth <= 0:
            return html[div_start:pos]
    return ""


def parse_current_weather(html: str, source_url: str) -> Dict[str, Any]:
    page_text = _strip_tags(html)
    title = _search([r"<title[^>]*>(.*?)</title>"], html)
    city = _search([r"Погода в\s+([^,]+)\s+сегодня"], title) or "Твери"
    widget_now = _extract_widget_now_block(html)
    widget_text = _strip_tags(widget_now) if widget_now else page_text

    temperature = _search(
        [r'<div class="now-weather">.*?<temperature-value[^>]*value="([-+]?\d+)"'],
        widget_now or html,
    )
    feels_like = _search(
        [r'<div class="now-feel">.*?<temperature-value[^>]*value="([-+]?\d+)"'],
        widget_now or html,
    )
    condition = _search([r'<div class="now-desc">\s*(.*?)\s*</div>'], widget_now or html) or "Актуальный прогноз"
    wind_speed = _search(
        [r'<div class="item-title">Ветер</div>.*?<div class="item-value">\s*<speed-value[^>]*value="([0-9.,]+)"'],
        widget_now or html,
    )
    wind_direction = _search(
        [r'<div class="item-title">Ветер</div>.*?<div class="item-measure"[^>]*>.*?<speed-value[^>]*>.*?</speed-value>\s*<br>\s*([^<]+)'],
        widget_now or html,
    )
    pressure = _search(
        [r'<div class="item-title">Давление</div>.*?<pressure-value[^>]*value="([0-9]{2,4})"'],
        widget_now or html,
    )
    humidity = _search(
        [r'<div class="item-title">Влажность</div>.*?<div class="item-value">([0-9]{1,3})</div>'],
        widget_now or html,
    )

    wind = ""
    if wind_speed:
        wind = f"{wind_speed} м/с"
        if wind_direction:
            wind += f", {wind_direction}"
    if humidity:
        humidity = f"{humidity} %"
    if pressure:
        pressure = f"{pressure} мм рт. ст."

    summary_parts = []
    if city:
        summary_parts.append(f"Погода в {city} сейчас")
    if temperature:
        summary_parts.append(f"температура {temperature} C")
    if feels_like:
        summary_parts.append(f"ощущается как {feels_like} C")
    if condition:
        summary_parts.append(condition.lower())
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
        "raw_text_excerpt": widget_text[:1200],
    }
