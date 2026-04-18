from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from src.analyzer.global_briefing import compose_global_briefing
from src.analyzer.relevance import ScoredArticle, score_articles
from src.fetcher import FetchSummary, fetch_all_sources_report
from src.fetcher.common import DomainRateLimiter, RobotsPolicy
from src.fetcher.models import RawArticle
from src.pipeline.partition import empty_fetch_summary
from src.utils.logging import get_logger
from src.utils.progress import emit_progress


async def run_global_fetch_stage(
    *,
    sources: list[Any],
    settings: Any,
    database_path: str,
    logger: Any,
    run_id: int,
    issue_number: int,
    client: Any,
    rate_limiter: DomainRateLimiter,
    robots_policy: RobotsPolicy,
    now: datetime | None,
    ignore_seen_db: bool,
    progress_callback: Callable[[str], None] | None,
    fetch_all_sources_report_fn: Callable[..., Any] = fetch_all_sources_report,
    logger_factory: Callable[..., Any] = get_logger,
) -> FetchSummary:
    if not sources:
        return empty_fetch_summary()

    try:
        return await fetch_all_sources_report_fn(
            sources=sources,
            settings=settings,
            database_path=database_path,
            logger=logger_factory(__name__, pipeline_stage="fetcher"),
            client=client,
            rate_limiter=rate_limiter,
            robots_policy=robots_policy,
            allow_robots_network_fallback=True,
            now=now,
            persist_to_db=False,
            use_database_seen_urls=not ignore_seen_db,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        logger.warning(
            "pipeline_global_fetch_failed",
            run_id=run_id,
            issue_number=issue_number,
            source_count=len(sources),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        emit_progress(
            progress_callback,
            f"Global macro fetch failed; continuing without that section: {exc}",
        )
        return empty_fetch_summary(
            sources_attempted=len(sources),
            sources_failed=len(sources),
        )


async def run_global_scoring_stage(
    *,
    articles: list[RawArticle],
    settings: Any,
    logger: Any,
    run_id: int,
    issue_number: int,
    progress_callback: Callable[[str], None] | None,
    now: datetime | None,
    score_articles_fn: Callable[..., Any] = score_articles,
    logger_factory: Callable[..., Any] = get_logger,
) -> list[ScoredArticle]:
    if not articles:
        return []

    try:
        return await score_articles_fn(
            articles,
            settings=settings,
            scoring_prompt_name="global_news_scoring.md",
            threshold=settings.global_news_relevance_threshold,
            logger=logger_factory(__name__, pipeline_stage="analyzer"),
            progress_callback=progress_callback,
            now=now,
        )
    except Exception as exc:
        logger.warning(
            "pipeline_global_scoring_failed",
            run_id=run_id,
            issue_number=issue_number,
            article_count=len(articles),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        emit_progress(
            progress_callback,
            f"Global macro scoring failed; continuing without that section: {exc}",
        )
        return []


async def run_global_briefing_stage(
    *,
    articles: list[ScoredArticle],
    settings: Any,
    logger: Any,
    run_id: int,
    issue_number: int,
    progress_callback: Callable[[str], None] | None,
    now: datetime | None,
    compose_global_briefing_fn: Callable[..., Any] = compose_global_briefing,
    logger_factory: Callable[..., Any] = get_logger,
) -> list[Any]:
    if not articles:
        return []

    try:
        return await compose_global_briefing_fn(
            articles,
            settings=settings,
            logger=logger_factory(__name__, pipeline_stage="analyzer"),
            progress_callback=progress_callback,
            now=now,
        )
    except Exception as exc:
        logger.warning(
            "pipeline_global_briefing_failed",
            run_id=run_id,
            issue_number=issue_number,
            article_count=len(articles),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        emit_progress(
            progress_callback,
            f"Global macro briefing composition failed; continuing without that section: {exc}",
        )
        return []


__all__ = [
    "run_global_briefing_stage",
    "run_global_fetch_stage",
    "run_global_scoring_stage",
]
