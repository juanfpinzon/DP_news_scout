from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from src.fetcher.common import parse_datetime

DEFAULT_CURRENT_WEEK_DAYS = 7


class PrioritizableArticle(Protocol):
    url: str
    published_at: str | None
    relevance_score: int


def resolve_reference_time(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def freshness_rank(
    published_at: str | None,
    *,
    now: datetime,
    current_week_days: int = DEFAULT_CURRENT_WEEK_DAYS,
) -> int:
    parsed = parse_datetime(published_at)
    if parsed is None:
        return -1

    age_hours = max(0.0, (now - parsed).total_seconds() / 3600)
    if age_hours <= 24:
        return 3
    if age_hours <= 72:
        return 2
    if age_hours <= current_week_days * 24:
        return 1
    return 0


def article_priority_key(
    article: PrioritizableArticle,
    *,
    now: datetime,
    current_week_days: int = DEFAULT_CURRENT_WEEK_DAYS,
) -> tuple[int, int, str, str]:
    parsed = parse_datetime(article.published_at)
    published_sort = parsed.isoformat() if parsed is not None else ""
    return (
        freshness_rank(
            article.published_at,
            now=now,
            current_week_days=current_week_days,
        ),
        article.relevance_score,
        published_sort,
        article.url,
    )


__all__ = [
    "DEFAULT_CURRENT_WEEK_DAYS",
    "article_priority_key",
    "freshness_rank",
    "resolve_reference_time",
]
