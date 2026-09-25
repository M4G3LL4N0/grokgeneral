from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")
_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = str(value).strip()
        if text.lower() in {"unknown", "unlimited", "never", "none", "null"}:
            return None
        if text.lower() == "now":
            return utc_now()
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp: {value}") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def parse_duration(value: str | int | float) -> timedelta:
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value}")
    amount, unit = match.groups()
    unit = unit.lower()
    if unit not in _UNITS:
        raise ValueError(f"unsupported duration unit: {unit}")
    return timedelta(seconds=float(amount) * _UNITS[unit])


def seconds_until(value: Any, now: datetime | None = None) -> float | None:
    target = parse_time(value)
    if target is None:
        return None
    current = now or utc_now()
    return (target - current).total_seconds()


def is_expired(value: Any, now: datetime | None = None) -> bool:
    remaining = seconds_until(value, now)
    return remaining is not None and remaining <= 0
