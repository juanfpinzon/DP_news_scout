from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.fetcher.common import parse_datetime
from src.fetcher.models import RawArticle, Source
from src.fetcher.search_fallback import load_effective_search_allowlist, resolve_allowed_publisher
from src.storage.db import ArticleRecord, load_articles, utc_now_iso


def load_raw_articles_from_storage(
    *,
    database_path: str,
    sources: list[Any],
    settings: Any,
    now: datetime | None = None,
) -> list[RawArticle]:
    stored_articles = load_articles(database_path)
    if not stored_articles:
        raise ValueError("no stored articles available in the database")

    reference_now = now or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(days=settings.reuse_seen_db_window_days)
    source_lookup = {
        str(source.name).strip().casefold(): source
        for source in sources
    }
    fallback_allowlist = load_effective_search_allowlist(source_registry=sources)
    recent_articles = [
        _article_record_to_raw_article(
            record,
            source_lookup=source_lookup,
            fallback_allowlist=fallback_allowlist,
        )
        for record in stored_articles
        if _record_is_recent_enough(
            record,
            cutoff=cutoff,
            source_lookup=source_lookup,
        )
    ]
    if not recent_articles:
        raise ValueError("no recent stored articles available in the database")

    recent_articles.sort(
        key=lambda article: (
            parse_datetime(article.published_at or article.fetched_at or "")
            or datetime.min.replace(tzinfo=timezone.utc),
            article.url,
        ),
        reverse=True,
    )
    return recent_articles


def _article_record_to_raw_article(
    record: ArticleRecord,
    *,
    source_lookup: dict[str, Any],
    fallback_allowlist: Any,
) -> RawArticle:
    source_url, category = _resolve_record_source_context(
        record,
        source_lookup=source_lookup,
        fallback_allowlist=fallback_allowlist,
    )
    return RawArticle(
        url=record.url,
        title=record.title,
        source=record.source,
        source_url=source_url,
        category=category,
        published_at=record.published_at,
        fetched_at=record.fetched_at or utc_now_iso(),
        summary=record.content_snippet,
        author=None,
        origin_source=record.origin_source,
        discovery_method=record.discovery_method,
    )


def _record_is_recent_enough(
    record: ArticleRecord,
    *,
    cutoff: datetime,
    source_lookup: dict[str, Any],
) -> bool:
    published_time = parse_datetime(record.published_at)
    if published_time is not None:
        return published_time >= cutoff

    if record.discovery_method == "search_fallback":
        return False

    source_name = record.origin_source or record.source
    source = source_lookup.get(source_name.strip().casefold())
    if getattr(source, "method", None) == "scrape":
        # Scraped entries without a resolved publication date are unreliable for
        # reuse mode: an old post can look "fresh" solely because it was fetched
        # recently. Exclude them until a real publication date is available.
        return False

    fetched_time = parse_datetime(record.fetched_at)
    return fetched_time is not None and fetched_time >= cutoff


def _resolve_record_source_context(
    record: ArticleRecord,
    *,
    source_lookup: dict[str, Any],
    fallback_allowlist: Any,
) -> tuple[str, str]:
    publisher_source = source_lookup.get(record.source.strip().casefold())
    if publisher_source is not None:
        return publisher_source.url, publisher_source.category

    if record.discovery_method == "search_fallback":
        publisher = resolve_allowed_publisher(
            record.url,
            allowlist=fallback_allowlist,
        )
        if publisher is not None:
            return f"https://{publisher.domain}/", publisher.group

    if record.origin_source:
        origin_source = source_lookup.get(record.origin_source.strip().casefold())
        if origin_source is not None:
            return origin_source.url, origin_source.category

    return "", "procurement"
