from datetime import datetime
from typing import Any, Dict


def build_weather_stub_message(task: Dict[str, Any]) -> str:
    payload = task.get("job_payload") if isinstance(task.get("job_payload"), dict) else {}
    city = str(payload.get("city") or "Не указан").strip() or "Не указан"
    period = str(payload.get("period") or "сейчас").strip() or "сейчас"
    created_by = str(payload.get("requested_by") or "agent").strip() or "agent"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "Погодная сводка\n"
        f"Город: {city}\n"
        f"Период: {period}\n"
        "Температура: +21 C\n"
        "Ощущается как: +19 C\n"
        "Осадки: без осадков\n"
        "Ветер: 3 м/с\n"
        "Влажность: 48%\n"
        f"Источник: weather_stub\n"
        f"Сформировано: {generated_at}\n"
        f"Запрос создан через: {created_by}"
    )


def execute_job(task: Dict[str, Any]) -> str:
    job_type = str(task.get("job_type") or "").strip().lower()
    if job_type == "weather_summary":
        return build_weather_stub_message(task)
    raise ValueError(f"Unsupported job_type: {job_type}")
