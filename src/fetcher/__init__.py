from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

from src.fetcher.common import DomainRateLimiter, RobotsPolicy, managed_async_client
from src.fetcher.dedup import deduplicate_articles, normalize_url
from src.fetcher.models import RawArticle, Source
from src.fetcher.registry import load_source_registry
from src.fetcher.rss import fetch_rss
from src.fetcher.search_fallback import search_fallback_articles
from src.fetcher.scraper import scrape_source
from src.storage.db import save_articles
from src.utils.config import Settings, load_config
from src.utils.logging import get_logger
from src.utils.progress import emit_progress

FetchResult = tuple[bool, list[RawArticle]]


@dataclass(slots=True)
class FetchSummary:
    articles: list[RawArticle]
    sources_attempted: int
    sources_succeeded: int
    sources_failed: int
    articles_found: int
    articles_deduplicated: int
    articles_saved: int

    @property
    def total_fetch_outage(self) -> bool:
        return self.sources_attempted > 0 and self.sources_succeeded == 0 and self.sources_failed > 0


async def fetch_all_sources(
    *,
    sources: list[Source] | None = None,
    settings: Settings | None = None,
    database_path: str | None = None,
    logger=None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    persist_to_db: bool = True,
    use_database_seen_urls: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> list[RawArticle]:
    summary = await fetch_all_sources_report(
        sources=sources,
        settings=settings,
        database_path=database_path,
        logger=logger,
        client=client,
        now=now,
        persist_to_db=persist_to_db,
        use_database_seen_urls=use_database_seen_urls,
        progress_callback=progress_callback,
    )
    return summary.articles


async def fetch_all_sources_report(
    *,
    sources: list[Source] | None = None,
    settings: Settings | None = None,
    database_path: str | None = None,
    logger=None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    persist_to_db: bool = True,
    use_database_seen_urls: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> FetchSummary:
    if settings is None or database_path is None:
        app_config = load_config()
        settings = settings or app_config.settings
        database_path = database_path or app_config.settings.database_path

    if sources is None:
        sources = load_source_registry(
            include_fallback_only=settings.search_fallback_enabled,
        )

    if logger is None:
        logger = get_logger(__name__, pipeline_stage="fetcher")

    semaphore = asyncio.Semaphore(settings.fetch_concurrency)
    rate_limiter = DomainRateLimiter(settings.rate_limit_seconds)
    robots_policy = RobotsPolicy()
    allow_robots_network_fallback = client is None

    async with managed_async_client(client, timeout_seconds=settings.request_timeout_seconds) as active_client:
        results = await asyncio.gather(
            *[
                _fetch_single_source(
                    source=source,
                    client=active_client,
                    semaphore=semaphore,
                    settings=settings,
                    rate_limiter=rate_limiter,
                    robots_policy=robots_policy,
                    allow_robots_network_fallback=allow_robots_network_fallback,
                    logger=logger,
                    now=now,
                    progress_callback=progress_callback,
                )
                for source in sources
            ]
        )

    raw_articles = [article for _, result in results for article in result]
    sources_succeeded, sources_failed = _count_fetch_results(results)
    deduplicated_articles = deduplicate_articles(
        raw_articles,
        database_path=database_path,
        dedup_window_days=settings.dedup_window_days,
        use_database_seen_urls=use_database_seen_urls,
    )
    stored_count = (
        save_articles(
            database_path,
            [article.to_record() for article in deduplicated_articles],
        )
        if persist_to_db
        else 0
    )

    logger.info(
        "fetch_all_sources_complete",
        sources_attempted=len(sources),
        sources_succeeded=sources_succeeded,
        sources_failed=sources_failed,
        articles_found=len(raw_articles),
        articles_deduplicated=len(deduplicated_articles),
        articles_saved=stored_count,
        persisted_to_db=persist_to_db,
        used_database_seen_urls=use_database_seen_urls,
    )
    emit_progress(
        progress_callback,
        "Fetch complete: "
        f"{sources_succeeded}/{len(sources)} sources succeeded, "
        f"{len(raw_articles)} raw {_pluralize(len(raw_articles), 'article')}, "
        f"{len(deduplicated_articles)} after dedup.",
    )
    return FetchSummary(
        articles=deduplicated_articles,
        sources_attempted=len(sources),
        sources_succeeded=sources_succeeded,
        sources_failed=sources_failed,
        articles_found=len(raw_articles),
        articles_deduplicated=len(deduplicated_articles),
        articles_saved=stored_count,
    )


async def _fetch_single_source(
    *,
    source: Source,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    settings: Settings,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    allow_robots_network_fallback: bool,
    logger,
    now: datetime | None,
    progress_callback: Callable[[str], None] | None,
) -> FetchResult:
    async with semaphore:
        fallback_enabled = _search_fallback_enabled(source=source, settings=settings)
        if _is_fallback_only_source(source=source, settings=settings):
            emit_progress(
                progress_callback,
                f"Source {source.name} is fallback-only; running Brave search fallback.",
            )
            try:
                articles = await _run_search_fallback(
                    source=source,
                    client=client,
                    settings=settings,
                    rate_limiter=rate_limiter,
                    robots_policy=robots_policy,
                    allow_robots_network_fallback=allow_robots_network_fallback,
                    logger=logger,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                logger.warning(
                    "source_search_fallback_failed",
                    source=source.name,
                    source_method=source.method,
                    source_url=source.url,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    fallback_only=True,
                )
                emit_progress(
                    progress_callback,
                    f"Source failed: {source.name} (search fallback) - {exc}",
                )
                return False, []

            _log_source_fetch_complete(
                logger=logger,
                source=source,
                article_count=len(articles),
                fetch_path="search_fallback_only",
            )
            emit_progress(
                progress_callback,
                f"Fetched {source.name} via search fallback: "
                f"{len(articles)} {_pluralize(len(articles), 'article')}.",
            )
            return True, articles

        try:
            articles = await _dispatch_source_fetch(
                source=source,
                client=client,
                settings=settings,
                rate_limiter=rate_limiter,
                robots_policy=robots_policy,
                allow_robots_network_fallback=allow_robots_network_fallback,
                now=now,
            )
        except Exception as exc:
            if fallback_enabled:
                emit_progress(
                    progress_callback,
                    f"Direct fetch failed for {source.name}; trying Brave search fallback.",
                )
                try:
                    articles = await _run_search_fallback(
                        source=source,
                        client=client,
                        settings=settings,
                        rate_limiter=rate_limiter,
                        robots_policy=robots_policy,
                        allow_robots_network_fallback=allow_robots_network_fallback,
                        logger=logger,
                        progress_callback=progress_callback,
                    )
                except Exception as fallback_exc:
                    logger.warning(
                        "source_search_fallback_failed",
                        source=source.name,
                        source_method=source.method,
                        source_url=source.url,
                        error=str(fallback_exc),
                        error_type=type(fallback_exc).__name__,
                        direct_fetch_error=str(exc),
                    )
                    emit_progress(
                        progress_callback,
                        f"Source failed: {source.name} ({source.method}) - {fallback_exc}",
                    )
                    return False, []

                _log_source_fetch_complete(
                    logger=logger,
                    source=source,
                    article_count=len(articles),
                    fetch_path="search_fallback_after_failure",
                )
                emit_progress(
                    progress_callback,
                    f"Fetched {source.name} via search fallback: "
                    f"{len(articles)} {_pluralize(len(articles), 'article')}.",
                )
                return True, articles

            logger.warning(
                "source_fetch_failed",
                source=source.name,
                source_method=source.method,
                source_url=source.url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            emit_progress(
                progress_callback,
                f"Source failed: {source.name} ({source.method}) - {exc}",
            )
            return False, []

        if articles or not fallback_enabled:
            _log_source_fetch_complete(
                logger=logger,
                source=source,
                article_count=len(articles),
                fetch_path="direct",
            )
            emit_progress(
                progress_callback,
                f"Fetched {source.name}: {len(articles)} {_pluralize(len(articles), 'article')}.",
            )
            return True, articles

        emit_progress(
            progress_callback,
            f"Source {source.name} returned 0 direct articles; trying Brave search fallback.",
        )
        try:
            fallback_articles = await _run_search_fallback(
                source=source,
                client=client,
                settings=settings,
                rate_limiter=rate_limiter,
                robots_policy=robots_policy,
                allow_robots_network_fallback=allow_robots_network_fallback,
                logger=logger,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            logger.warning(
                "source_search_fallback_failed",
                source=source.name,
                source_method=source.method,
                source_url=source.url,
                error=str(exc),
                error_type=type(exc).__name__,
                direct_fetch_succeeded=True,
            )
            _log_source_fetch_complete(
                logger=logger,
                source=source,
                article_count=0,
                fetch_path="direct_empty_fallback_failed",
            )
            emit_progress(
                progress_callback,
                f"Fetched {source.name}: 0 articles.",
            )
            return True, articles

        _log_source_fetch_complete(
            logger=logger,
            source=source,
            article_count=len(fallback_articles),
            fetch_path="search_fallback_after_empty",
        )
        emit_progress(
            progress_callback,
            f"Fetched {source.name} via search fallback: "
            f"{len(fallback_articles)} {_pluralize(len(fallback_articles), 'article')}.",
        )
        return True, fallback_articles


async def _dispatch_source_fetch(
    *,
    source: Source,
    client: httpx.AsyncClient,
    settings: Settings,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    allow_robots_network_fallback: bool,
    now: datetime | None,
) -> list[RawArticle]:
    shared_kwargs = {
        "client": client,
        "allow_robots_network_fallback": allow_robots_network_fallback,
        "lookback_hours": settings.rss_lookback_hours,
        "max_articles": settings.max_articles_per_source,
        "timeout_seconds": settings.request_timeout_seconds,
        "rate_limiter": rate_limiter,
        "robots_policy": robots_policy,
        "now": now,
    }

    if source.method == "rss":
        return await fetch_rss(source, **shared_kwargs)
    if source.method == "scrape":
        return await scrape_source(source, **shared_kwargs)

    raise ValueError(f"Unsupported source method: {source.method}")


async def _run_search_fallback(
    *,
    source: Source,
    client: httpx.AsyncClient,
    settings: Settings,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    allow_robots_network_fallback: bool,
    logger,
    progress_callback: Callable[[str], None] | None = None,
) -> list[RawArticle]:
    return await search_fallback_articles(
        source,
        client=client,
        settings=settings,
        rate_limiter=rate_limiter,
        robots_policy=robots_policy,
        allow_robots_network_fallback=allow_robots_network_fallback,
        logger=logger,
        progress_callback=progress_callback,
    )


def _search_fallback_enabled(*, source: Source, settings: Settings) -> bool:
    if not settings.search_fallback_enabled:
        return False
    if source.active:
        if source.fallback_search.enabled_explicit:
            return source.fallback_search.enabled
        return True
    return (
        source.fallback_search.enabled
        and source.fallback_search.include_when_inactive
    )


def _is_fallback_only_source(*, source: Source, settings: Settings) -> bool:
    return (
        not source.active
        and _search_fallback_enabled(source=source, settings=settings)
        and source.fallback_search.include_when_inactive
    )


def _log_source_fetch_complete(
    *,
    logger,
    source: Source,
    article_count: int,
    fetch_path: str,
) -> None:
    logger.info(
        "source_fetch_complete",
        source=source.name,
        source_method=source.method,
        source_url=source.url,
        article_count=article_count,
        fetch_path=fetch_path,
    )


def _count_fetch_results(results: Sequence[FetchResult]) -> tuple[int, int]:
    succeeded = sum(1 for success, _ in results if success)
    failed = len(results) - succeeded
    return succeeded, failed


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


__all__ = [
    "RawArticle",
    "Source",
    "FetchSummary",
    "deduplicate_articles",
    "fetch_all_sources",
    "fetch_all_sources_report",
    "fetch_rss",
    "load_source_registry",
    "normalize_url",
    "scrape_source",
]
