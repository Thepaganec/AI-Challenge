from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_iso_datetime(value: str) -> datetime:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError("datetime value is required")
    formats = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    raise ValueError("Unsupported datetime format. Use YYYY-MM-DD HH:MM")


def format_iso_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_time_points(time_points: Any) -> List[Dict[str, int]]:
    if not isinstance(time_points, list) or not time_points:
        raise ValueError("schedule.time_points must be a non-empty list")
    parsed: List[Dict[str, int]] = []
    for item in time_points:
        if not isinstance(item, dict):
            raise ValueError("each time point must be an object")
        hour = int(item.get("hour"))
        minute = int(item.get("minute"))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("time point hour/minute out of range")
        parsed.append({"hour": hour, "minute": minute})
    parsed.sort(key=lambda row: (row["hour"], row["minute"]))
    return parsed


def validate_schedule_definition(schedule_type: str, schedule: Dict[str, Any]) -> Dict[str, Any]:
    clean_type = str(schedule_type or "").strip().lower()
    payload = dict(schedule or {})

    if clean_type == "once":
        run_at = parse_iso_datetime(str(payload.get("run_at") or ""))
        return {"run_at": format_iso_datetime(run_at)}

    if clean_type == "interval":
        unit = str(payload.get("unit") or "").strip().lower()
        value = int(payload.get("every") or 0)
        if unit not in {"minutes", "hours"}:
            raise ValueError("interval unit must be 'minutes' or 'hours'")
        if value <= 0:
            raise ValueError("interval every must be > 0")
        return {"unit": unit, "every": value}

    if clean_type == "daily":
        return {"time_points": _parse_time_points(payload.get("time_points"))}

    if clean_type == "weekly":
        days_raw = payload.get("days")
        if not isinstance(days_raw, list) or not days_raw:
            raise ValueError("weekly schedule.days must be a non-empty list")
        days: List[str] = []
        for item in days_raw:
            day = str(item or "").strip().lower()
            if day not in WEEKDAY_MAP:
                raise ValueError(f"unsupported weekday: {item}")
            if day not in days:
                days.append(day)
        return {"days": days, "time_points": _parse_time_points(payload.get("time_points"))}

    raise ValueError("schedule_type must be one of: once, interval, daily, weekly")


def compute_next_run(schedule_type: str, schedule: Dict[str, Any], *, now: Optional[datetime] = None, last_run: Optional[str] = None) -> Optional[str]:
    ref = now or datetime.now()
    clean_type = str(schedule_type or "").strip().lower()

    if clean_type == "once":
        run_at = parse_iso_datetime(str(schedule.get("run_at") or ""))
        return format_iso_datetime(run_at)

    if clean_type == "interval":
        every = int(schedule.get("every") or 0)
        unit = str(schedule.get("unit") or "").strip().lower()
        delta = timedelta(minutes=every) if unit == "minutes" else timedelta(hours=every)
        if last_run:
            base = parse_iso_datetime(last_run)
            next_run = base + delta
            while next_run <= ref:
                next_run += delta
            return format_iso_datetime(next_run)
        return format_iso_datetime(ref + delta)

    if clean_type == "daily":
        time_points = _parse_time_points(schedule.get("time_points"))
        for shift in range(0, 8):
            candidate_date = (ref + timedelta(days=shift)).date()
            for point in time_points:
                candidate = datetime.combine(candidate_date, datetime.min.time()).replace(hour=point["hour"], minute=point["minute"])
                if candidate > ref:
                    return format_iso_datetime(candidate)
        return None

    if clean_type == "weekly":
        days = [WEEKDAY_MAP[str(day)] for day in schedule.get("days", [])]
        time_points = _parse_time_points(schedule.get("time_points"))
        for shift in range(0, 15):
            candidate_date = (ref + timedelta(days=shift)).date()
            if candidate_date.weekday() not in days:
                continue
            for point in time_points:
                candidate = datetime.combine(candidate_date, datetime.min.time()).replace(hour=point["hour"], minute=point["minute"])
                if candidate > ref:
                    return format_iso_datetime(candidate)
        return None

    return None


def is_due(next_run_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    if not next_run_at:
        return False
    return parse_iso_datetime(next_run_at) <= (now or datetime.now())
