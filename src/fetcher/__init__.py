from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

import httpx

from src.fetcher.common import DomainRateLimiter, RobotsPolicy, managed_async_client
from src.fetcher.dedup import deduplicate_articles, normalize_url
from src.fetcher.models import RawArticle, Source
from src.fetcher.registry import load_source_registry
from src.fetcher.rss import fetch_rss
from src.fetcher.scraper import scrape_source
from src.storage.db import save_articles
from src.utils.config import Settings, load_config
from src.utils.logging import get_logger

FetchResult = tuple[bool, list[RawArticle]]


async def fetch_all_sources(
    *,
    sources: list[Source] | None = None,
    settings: Settings | None = None,
    database_path: str | None = None,
    logger=None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> list[RawArticle]:
    if settings is None or database_path is None:
        app_config = load_config()
        settings = settings or app_config.settings
        database_path = database_path or app_config.settings.database_path

    if sources is None:
        sources = load_source_registry()

    if logger is None:
        logger = get_logger(__name__, pipeline_stage="fetcher")

    semaphore = asyncio.Semaphore(settings.fetch_concurrency)
    rate_limiter = DomainRateLimiter(settings.rate_limit_seconds)
    robots_policy = RobotsPolicy()

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
                    logger=logger,
                    now=now,
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
    )
    stored_count = save_articles(
        database_path,
        [article.to_record() for article in deduplicated_articles],
    )

    logger.info(
        "fetch_all_sources_complete",
        sources_attempted=len(sources),
        sources_succeeded=sources_succeeded,
        sources_failed=sources_failed,
        articles_found=len(raw_articles),
        articles_deduplicated=len(deduplicated_articles),
        articles_saved=stored_count,
    )
    return deduplicated_articles


async def _fetch_single_source(
    *,
    source: Source,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    settings: Settings,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    logger,
    now: datetime | None,
) -> FetchResult:
    async with semaphore:
        try:
            articles = await _dispatch_source_fetch(
                source=source,
                client=client,
                settings=settings,
                rate_limiter=rate_limiter,
                robots_policy=robots_policy,
                now=now,
            )
        except Exception as exc:
            logger.warning(
                "source_fetch_failed",
                source=source.name,
                source_method=source.method,
                source_url=source.url,
                error=str(exc),
            )
            return False, []

        logger.info(
            "source_fetch_complete",
            source=source.name,
            source_method=source.method,
            article_count=len(articles),
        )
        return True, articles


async def _dispatch_source_fetch(
    *,
    source: Source,
    client: httpx.AsyncClient,
    settings: Settings,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    now: datetime | None,
) -> list[RawArticle]:
    shared_kwargs = {
        "client": client,
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


def _count_fetch_results(results: Sequence[FetchResult]) -> tuple[int, int]:
    succeeded = sum(1 for success, _ in results if success)
    failed = len(results) - succeeded
    return succeeded, failed


__all__ = [
    "RawArticle",
    "Source",
    "deduplicate_articles",
    "fetch_all_sources",
    "fetch_rss",
    "load_source_registry",
    "normalize_url",
    "scrape_source",
]
