from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE_ALIASES = {
    "central european time": "Europe/Madrid",
    "cet": "Europe/Madrid",
    "cest": "Europe/Madrid",
}


def format_display_date(settings: Any, *, now: datetime | None = None) -> str:
    reference_date = _resolve_issue_reference_date(
        now=now,
        timezone_name=getattr(settings, "timezone", None),
    )
    return f"{reference_date.strftime('%B')} {reference_date.day}, {reference_date.year}"


def resolve_issue_number(
    settings: Any,
    *,
    fallback: int,
    now: datetime | None = None,
) -> int:
    override = getattr(settings, "issue_number_override", None)
    if override is not None:
        return int(override)

    start_date = _coerce_issue_start_date(getattr(settings, "issue_number_start_date", None))
    if start_date is not None:
        reference_date = _resolve_issue_reference_date(
            now=now,
            timezone_name=getattr(settings, "timezone", None),
        )
        if reference_date < start_date:
            return 0
        return ((reference_date - start_date).days // 7) + 1

    return fallback


def _coerce_issue_start_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    normalized = str(value).strip()
    if normalized == "" or normalized.lower() in {"none", "null"}:
        return None
    return date.fromisoformat(normalized)


def _resolve_issue_reference_date(*, now: datetime | None, timezone_name: str | None) -> date:
    active_now = now or datetime.now(timezone.utc)
    if active_now.tzinfo is None:
        active_now = active_now.replace(tzinfo=timezone.utc)
    return active_now.astimezone(_resolve_issue_timezone(timezone_name)).date()


def _resolve_issue_timezone(timezone_name: str | None):
    normalized = (timezone_name or "").strip()
    if not normalized:
        return timezone.utc

    candidates = [normalized]
    alias = TIMEZONE_ALIASES.get(normalized.casefold())
    if alias is not None:
        candidates.insert(0, alias)

    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    return timezone.utc
