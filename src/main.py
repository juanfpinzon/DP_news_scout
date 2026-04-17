from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.analyzer.digest import Digest, compose_digest
from src.analyzer.global_briefing import compose_global_briefing
from src.analyzer.relevance import ScoredArticle, score_articles
from src.fetcher.common import parse_datetime
from src.fetcher.dedup import normalize_url
from src.fetcher.search_fallback import load_effective_search_allowlist, resolve_allowed_publisher
from src.fetcher import FetchSummary, RawArticle, fetch_all_sources_report, load_source_registry
from src.renderer import render_digest, render_plaintext
from src.sender import send_digest
from src.storage.db import (
    ArticleRecord,
    PipelineRunRecord,
    initialize_database,
    load_articles,
    log_run,
    save_articles,
    utc_now_iso,
)
from src.utils.config import AppConfig, load_config
from src.utils.logging import configure_logging, get_logger
from src.utils.progress import build_stdout_progress_callback, emit_progress

DEFAULT_SUBJECT_PREFIX = "Digital Procurement News Scout"
GLOBAL_NEWS_CATEGORY = "global_news"
TIMEZONE_ALIASES = {
    "central european time": "Europe/Madrid",
    "cet": "Europe/Madrid",
    "cest": "Europe/Madrid",
}


@dataclass(slots=True)
class PipelineResult:
    run_id: int
    issue_number: int
    status: str
    started_at: str
    completed_at: str
    sources_fetched: int
    articles_found: int
    relevant_articles: int
    articles_included: int
    email_sent: bool
    subject: str
    dry_run: bool
    error: str | None = None


def run_pipeline(
    *,
    config: AppConfig | None = None,
    now: datetime | None = None,
    ignore_seen_db: bool = False,
    reuse_seen_db: bool = False,
    subject_suffix: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> PipelineResult:
    _validate_fetch_mode(ignore_seen_db=ignore_seen_db, reuse_seen_db=reuse_seen_db)
    config = config or load_config()
    initialize_database(config.settings.database_path)
    logger = get_logger(__name__, pipeline_stage="pipeline")
    started_at = utc_now_iso()
    date_label = format_display_date(config.settings, now=now)
    default_subject = _build_digest_subject(
        issue_number=resolve_issue_number(config.settings, fallback=1, now=now),
        date_label=date_label,
        subject_suffix=subject_suffix,
    )

    run_id = log_run(
        config.settings.database_path,
        PipelineRunRecord(
            started_at=started_at,
            status="started",
            sources_fetched=0,
        ),
    )
    issue_number = resolve_issue_number(config.settings, fallback=run_id, now=now)
    default_subject = _build_digest_subject(
        issue_number=issue_number,
        date_label=date_label,
        subject_suffix=subject_suffix,
    )

    try:
        sources = load_source_registry(
            include_fallback_only=config.settings.search_fallback_enabled,
        )
        procurement_sources, global_news_sources = _partition_sources_by_category(sources)
    except Exception as exc:
        error = f"source registry stage failed: {exc}"
        _report_progress(progress_callback, f"Source registry failed: {exc}")
        _log_pipeline_stage_failure(
            logger=logger,
            run_id=run_id,
            issue_number=issue_number,
            stage="source_registry",
            error=error,
            exc=exc,
        )
        return _finalize_pipeline_run(
            config=config,
            run_id=run_id,
            issue_number=issue_number,
            started_at=started_at,
            subject=default_subject,
            sources_fetched=0,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            dry_run=config.settings.dry_run,
            status="failed",
            error=error,
            logger=logger,
        )

    _report_progress(
        progress_callback,
        "Loaded "
        f"{len(sources)} configured {_pluralize(len(sources), 'source')} for issue #{issue_number} "
        f"({len(procurement_sources)} procurement, {len(global_news_sources)} global macro).",
    )
    logger.info(
        "pipeline_started",
        run_id=run_id,
        issue_number=issue_number,
        source_count=len(sources),
        procurement_source_count=len(procurement_sources),
        global_news_source_count=len(global_news_sources),
        recipient_group=config.default_recipient_group,
        recipient_count=len(config.recipients),
        dry_run=config.settings.dry_run,
        ignore_seen_db=ignore_seen_db,
        reuse_seen_db=reuse_seen_db,
        timeout_seconds=config.settings.pipeline_timeout,
    )

    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_pipeline_async(
                    config=config,
                    run_id=run_id,
                    issue_number=issue_number,
                    started_at=started_at,
                    procurement_sources=procurement_sources,
                    global_news_sources=global_news_sources,
                    logger=logger,
                    now=now,
                    ignore_seen_db=ignore_seen_db,
                    reuse_seen_db=reuse_seen_db,
                    subject_suffix=subject_suffix,
                    progress_callback=progress_callback,
                ),
                timeout=config.settings.pipeline_timeout,
            )
        )
    except asyncio.TimeoutError:
        error = (
            f"Pipeline timed out after {config.settings.pipeline_timeout} seconds"
        )
        _report_progress(progress_callback, error)
        _log_pipeline_stage_failure(
            logger=logger,
            run_id=run_id,
            issue_number=issue_number,
            stage="pipeline",
            error=error,
            timeout_seconds=config.settings.pipeline_timeout,
        )
        return _finalize_pipeline_run(
            config=config,
            run_id=run_id,
            issue_number=issue_number,
            status="failed",
            started_at=started_at,
            sources_fetched=0,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            subject=default_subject,
            dry_run=config.settings.dry_run,
            error=error,
            logger=logger,
        )
    except Exception as exc:
        error = f"pipeline orchestration failed: {exc}"
        _report_progress(progress_callback, f"Pipeline orchestration failed: {exc}")
        _log_pipeline_stage_failure(
            logger=logger,
            run_id=run_id,
            issue_number=issue_number,
            stage="pipeline",
            error=error,
            exc=exc,
        )
        return _finalize_pipeline_run(
            config=config,
            run_id=run_id,
            issue_number=issue_number,
            started_at=started_at,
            subject=default_subject,
            sources_fetched=0,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            dry_run=config.settings.dry_run,
            status="failed",
            error=error,
            logger=logger,
        )

    return result


async def _run_pipeline_async(
    *,
    config: AppConfig,
    run_id: int,
    issue_number: int,
    started_at: str,
    procurement_sources: list[Any],
    global_news_sources: list[Any],
    logger: Any,
    now: datetime | None,
    ignore_seen_db: bool,
    reuse_seen_db: bool,
    subject_suffix: str | None,
    progress_callback: Callable[[str], None] | None,
) -> PipelineResult:
    date_label = format_display_date(config.settings, now=now)
    subject = _build_digest_subject(
        issue_number=issue_number,
        date_label=date_label,
        subject_suffix=subject_suffix,
    )
    all_sources = [*procurement_sources, *global_news_sources]
    procurement_progress = _prefixed_progress_callback(progress_callback, "Procurement track")
    global_progress = _prefixed_progress_callback(progress_callback, "Global macro track")
    fetch_summary = FetchSummary(
        articles=[],
        sources_attempted=len(all_sources),
        sources_succeeded=0,
        sources_failed=0,
        articles_found=0,
        articles_deduplicated=0,
        articles_saved=0,
    )
    procurement_fetch_summary = _empty_fetch_summary(
        sources_attempted=len(procurement_sources),
    )
    global_fetch_summary = _empty_fetch_summary(
        sources_attempted=len(global_news_sources),
    )
    raw_articles: list[RawArticle] = []
    procurement_raw_articles: list[RawArticle] = []
    global_raw_articles: list[RawArticle] = []
    scored_articles: list[ScoredArticle] = []
    procurement_scored_articles: list[ScoredArticle] = []
    global_scored_articles: list[ScoredArticle] = []
    digest: Digest | None = None
    articles_included = 0
    email_sent = False
    error: str | None = None
    status = "success"

    try:
        if reuse_seen_db:
            _report_progress(
                progress_callback,
                "Loading stored articles from SQLite instead of fetching live sources.",
            )
            raw_articles = load_raw_articles_from_storage(
                database_path=config.settings.database_path,
                sources=all_sources,
                settings=config.settings,
                now=now,
            )
            procurement_raw_articles, global_raw_articles = _partition_articles_by_category(raw_articles)
            fetch_summary = FetchSummary(
                articles=raw_articles,
                sources_attempted=len(all_sources),
                sources_succeeded=0,
                sources_failed=0,
                articles_found=len(raw_articles),
                articles_deduplicated=len(raw_articles),
                articles_saved=0,
            )
            logger.info(
                "pipeline_reusing_stored_articles",
                run_id=run_id,
                issue_number=issue_number,
                article_count=len(raw_articles),
                procurement_articles=len(procurement_raw_articles),
                global_news_articles=len(global_raw_articles),
            )
            _report_progress(
                progress_callback,
                "Loaded "
                f"{len(raw_articles)} stored {_pluralize(len(raw_articles), 'article')} from SQLite "
                f"({len(procurement_raw_articles)} procurement, {len(global_raw_articles)} global macro).",
            )
        else:
            _report_progress(
                progress_callback,
                "Fetching live articles from "
                f"{len(procurement_sources)} procurement {_pluralize(len(procurement_sources), 'source')} "
                f"and {len(global_news_sources)} global macro {_pluralize(len(global_news_sources), 'source')}.",
            )
            procurement_fetch_summary, global_fetch_summary = await asyncio.gather(
                fetch_all_sources_report(
                    sources=procurement_sources,
                    settings=config.settings,
                    database_path=config.settings.database_path,
                    logger=get_logger(__name__, pipeline_stage="fetcher"),
                    now=now,
                    persist_to_db=False,
                    use_database_seen_urls=not ignore_seen_db,
                    progress_callback=procurement_progress,
                ),
                _run_global_fetch_stage(
                    sources=global_news_sources,
                    settings=config.settings,
                    database_path=config.settings.database_path,
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    now=now,
                    ignore_seen_db=ignore_seen_db,
                    progress_callback=global_progress,
                ),
            )
            fetch_summary = _merge_fetch_summaries(
                procurement_fetch_summary,
                global_fetch_summary,
            )
            raw_articles = fetch_summary.articles
            procurement_raw_articles, global_raw_articles = _partition_articles_by_category(raw_articles)
    except Exception as exc:
        status = "failed"
        stage_name = "stored_article_reuse" if reuse_seen_db else "fetcher"
        error = f"{stage_name} stage failed: {exc}"
        _report_progress(progress_callback, error)
        _log_pipeline_stage_failure(
            logger=logger,
            run_id=run_id,
            issue_number=issue_number,
            stage=stage_name,
            error=error,
            exc=exc,
        )
        return _finalize_pipeline_run(
            config=config,
            run_id=run_id,
            issue_number=issue_number,
            started_at=started_at,
            subject=subject,
            sources_fetched=fetch_summary.sources_succeeded,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            dry_run=config.settings.dry_run,
            status=status,
            error=error,
            logger=logger,
        )

    if (
        not reuse_seen_db
        and procurement_sources
        and _summary_has_total_fetch_outage(procurement_fetch_summary)
    ):
        status = "failed"
        error = "fetcher stage failed: all configured sources failed to fetch"
        _report_progress(progress_callback, error)
        _log_pipeline_stage_failure(
            logger=logger,
            run_id=run_id,
            issue_number=issue_number,
            stage="fetcher",
            error=error,
            sources_attempted=fetch_summary.sources_attempted,
            sources_failed=fetch_summary.sources_failed,
        )
        return _finalize_pipeline_run(
            config=config,
            run_id=run_id,
            issue_number=issue_number,
            started_at=started_at,
            subject=subject,
            sources_fetched=fetch_summary.sources_succeeded,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            dry_run=config.settings.dry_run,
            status=status,
            error=error,
            logger=logger,
        )

    if raw_articles:
        try:
            _report_progress(
                progress_callback,
                "Scoring "
                f"{len(procurement_raw_articles)} procurement and {len(global_raw_articles)} global macro "
                f"{_pluralize(len(raw_articles), 'article')} with {config.settings.llm_scoring_model}.",
            )
            procurement_scored_articles, global_scored_articles = await asyncio.gather(
                score_articles(
                    procurement_raw_articles,
                    settings=config.settings,
                    logger=get_logger(__name__, pipeline_stage="analyzer"),
                    progress_callback=procurement_progress,
                    now=now,
                ),
                _run_global_scoring_stage(
                    articles=global_raw_articles,
                    settings=config.settings,
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    progress_callback=global_progress,
                    now=now,
                ),
            )
            scored_articles = [*procurement_scored_articles, *global_scored_articles]
        except Exception as exc:
            status = "failed"
            error = f"analyzer relevance stage failed: {exc}"
            _report_progress(progress_callback, error)
            _log_pipeline_stage_failure(
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
                stage="analyzer_relevance",
                error=error,
                exc=exc,
            )
            return _finalize_pipeline_run(
                config=config,
                run_id=run_id,
                issue_number=issue_number,
                started_at=started_at,
                subject=subject,
                sources_fetched=fetch_summary.sources_succeeded,
                articles_found=len(raw_articles),
                relevant_articles=0,
                articles_included=0,
                email_sent=False,
                dry_run=config.settings.dry_run,
                status=status,
                error=error,
                logger=logger,
            )

    if procurement_scored_articles:
        try:
            _report_progress(
                progress_callback,
                "Composing digest from "
                f"{len(procurement_scored_articles)} relevant procurement {_pluralize(len(procurement_scored_articles), 'article')} "
                f"with {config.settings.llm_digest_model}.",
            )
            digest, global_briefing = await asyncio.gather(
                compose_digest(
                    procurement_scored_articles,
                    settings=config.settings,
                    logger=get_logger(__name__, pipeline_stage="analyzer"),
                    progress_callback=procurement_progress,
                    now=now,
                ),
                _run_global_briefing_stage(
                    articles=global_scored_articles,
                    settings=config.settings,
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    progress_callback=global_progress,
                    now=now,
                ),
            )
            digest.global_briefing = global_briefing
        except Exception as exc:
            status = "failed"
            error = f"digest composition stage failed: {exc}"
            _report_progress(progress_callback, error)
            _log_pipeline_stage_failure(
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
                stage="digest_composition",
                error=error,
                exc=exc,
            )
            return _finalize_pipeline_run(
                config=config,
                run_id=run_id,
                issue_number=issue_number,
                started_at=started_at,
                subject=subject,
                sources_fetched=fetch_summary.sources_succeeded,
                articles_found=len(raw_articles),
                relevant_articles=len(procurement_scored_articles) + len(global_scored_articles),
                articles_included=0,
                email_sent=False,
                dry_run=config.settings.dry_run,
                status=status,
                error=error,
                logger=logger,
            )

        try:
            _report_progress(
                progress_callback,
                "Rendering HTML and plain-text digest output.",
            )
            html = render_digest(
                digest,
                issue_number=issue_number,
                date=date_label,
                max_width_px=config.settings.email_max_width_px,
            )
            plaintext = render_plaintext(digest, issue_number=issue_number, date=date_label)
        except Exception as exc:
            status = "failed"
            error = f"renderer stage failed: {exc}"
            _report_progress(progress_callback, error)
            _log_pipeline_stage_failure(
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
                stage="renderer",
                error=error,
                exc=exc,
            )
            return _finalize_pipeline_run(
                config=config,
                run_id=run_id,
                issue_number=issue_number,
                started_at=started_at,
                subject=subject,
                sources_fetched=fetch_summary.sources_succeeded,
                articles_found=len(raw_articles),
                relevant_articles=len(scored_articles),
                articles_included=0,
                email_sent=False,
                dry_run=config.settings.dry_run,
                status=status,
                error=error,
                logger=logger,
            )

        articles_included = _count_digest_articles(digest)
        _report_progress(
            progress_callback,
            f"Renderer complete: {articles_included} {_pluralize(articles_included, 'item')} included in the digest.",
        )
    else:
        _report_progress(
            progress_callback,
            "No procurement articles cleared the relevance threshold. Building the no-news digest.",
        )
        html, plaintext = _build_no_news_email(issue_number=issue_number, date_label=date_label)
        subject = _build_no_news_subject(
            issue_number=issue_number,
            date_label=date_label,
            subject_suffix=subject_suffix,
        )

    if config.settings.dry_run:
        _report_progress(progress_callback, "Send stage skipped because dry-run is enabled.")
        logger.info(
            "pipeline_send_skipped",
            run_id=run_id,
            issue_number=issue_number,
            reason="dry_run",
            subject=subject,
        )
    else:
        try:
            _report_progress(progress_callback, "Starting email delivery.")
            send_kwargs: dict[str, Any] = {
                "config": config,
                "run_id": run_id,
                "issue_number": issue_number,
            }
            if progress_callback is not None:
                send_kwargs["progress_callback"] = progress_callback
            email_sent = send_digest(
                html,
                plaintext,
                subject,
                **send_kwargs,
            )
        except Exception as exc:
            status = "failed"
            error = f"sender stage failed: {exc}"
            _report_progress(progress_callback, error)
            _log_pipeline_stage_failure(
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
                stage="sender",
                error=error,
                exc=exc,
            )
        else:
            if not email_sent:
                status = "failed"
                error = "sender stage returned unsuccessful delivery status"
                _report_progress(progress_callback, error)
                _log_pipeline_stage_failure(
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    stage="sender",
                    error=error,
                )

    if status == "success" and email_sent:
        try:
            _report_progress(progress_callback, "Persisting fetched article state to SQLite.")
            _persist_seen_articles(
                database_path=config.settings.database_path,
                raw_articles=raw_articles,
                scored_articles=scored_articles,
                digest=digest,
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
            )
            _report_progress(progress_callback, "SQLite article persistence complete.")
        except Exception as exc:
            logger.warning(
                "pipeline_article_persistence_failed",
                run_id=run_id,
                issue_number=issue_number,
                error=str(exc),
            )

    return _finalize_pipeline_run(
        config=config,
        run_id=run_id,
        issue_number=issue_number,
        started_at=started_at,
        subject=subject,
        sources_fetched=fetch_summary.sources_succeeded,
        articles_found=len(raw_articles),
        relevant_articles=len(procurement_scored_articles) + len(global_scored_articles),
        articles_included=articles_included,
        email_sent=email_sent,
        dry_run=config.settings.dry_run,
        status=status,
        error=error,
        logger=logger,
    )


def _finalize_pipeline_run(
    *,
    config: AppConfig,
    run_id: int,
    issue_number: int,
    started_at: str,
    subject: str,
    sources_fetched: int,
    articles_found: int,
    relevant_articles: int,
    articles_included: int,
    email_sent: bool,
    dry_run: bool,
    status: str,
    error: str | None,
    logger: Any,
) -> PipelineResult:
    completed_at = utc_now_iso()
    log_run(
        config.settings.database_path,
        PipelineRunRecord(
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            sources_fetched=sources_fetched,
            articles_found=articles_found,
            articles_included=articles_included,
            error_log=error,
        ),
        run_id=run_id,
    )

    log_method = logger.info if status == "success" else logger.error
    log_method(
        "pipeline_completed",
        run_id=run_id,
        issue_number=issue_number,
        status=status,
        sources_fetched=sources_fetched,
        articles_found=articles_found,
        relevant_articles=relevant_articles,
        articles_included=articles_included,
        email_sent=email_sent,
        dry_run=dry_run,
        subject=subject,
        error=error,
    )

    return PipelineResult(
        run_id=run_id,
        issue_number=issue_number,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        sources_fetched=sources_fetched,
        articles_found=articles_found,
        relevant_articles=relevant_articles,
        articles_included=articles_included,
        email_sent=email_sent,
        subject=subject,
        dry_run=dry_run,
        error=error,
    )


def _log_pipeline_stage_failure(
    *,
    logger: Any,
    run_id: int,
    issue_number: int,
    stage: str,
    error: str,
    exc: Exception | None = None,
    **context: Any,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "issue_number": issue_number,
        "stage": stage,
        "error": error,
        **context,
    }
    if exc is not None:
        payload["error_type"] = type(exc).__name__
    logger.error("pipeline_stage_failed", **payload)


def _count_digest_articles(digest: Digest) -> int:
    return (
        1
        + len(digest.key_developments)
        + len(digest.on_our_radar)
        + len(digest.quick_hits)
        + len(digest.global_briefing)
    )


def _persist_seen_articles(
    *,
    database_path: str,
    raw_articles: list[RawArticle],
    scored_articles: list[ScoredArticle],
    digest: Digest | None,
    logger: Any,
    run_id: int,
    issue_number: int,
) -> None:
    if not raw_articles:
        return

    scored_by_url = {
        article.url: article for article in scored_articles
    }
    included_urls = _collect_included_urls(digest) if digest is not None else set()
    records = []

    for article in raw_articles:
        record = article.to_record()
        scored = scored_by_url.get(article.url)
        if scored is not None:
            record.relevance_score = float(scored.relevance_score)
        record.included_in_digest = article.url in included_urls
        records.append(record)

    stored_count = save_articles(database_path, records)
    logger.info(
        "pipeline_articles_persisted",
        run_id=run_id,
        issue_number=issue_number,
        article_count=stored_count,
    )


def _collect_included_urls(digest: Digest) -> set[str]:
    return {
        digest.top_story.url,
        *(item.url for item in digest.key_developments),
        *(item.url for item in digest.on_our_radar),
        *(item.url for item in digest.quick_hits),
        *(item.url for item in digest.global_briefing),
    }


def _build_digest_subject(
    *,
    issue_number: int,
    date_label: str,
    subject_suffix: str | None = None,
) -> str:
    subject = f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | Issue #{issue_number}"
    return _append_subject_suffix(subject, subject_suffix)


def _build_no_news_subject(
    *,
    issue_number: int,
    date_label: str,
    subject_suffix: str | None = None,
) -> str:
    subject = (
        f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | No major updates | Issue #{issue_number}"
    )
    return _append_subject_suffix(subject, subject_suffix)


def _append_subject_suffix(subject: str, subject_suffix: str | None) -> str:
    if not subject_suffix:
        return subject
    return f"{subject} | {subject_suffix}"


def _build_no_news_email(*, issue_number: int, date_label: str) -> tuple[str, str]:
    html = (
        "<html><body>"
        "<h1>Digital Procurement News Scout</h1>"
        f"<p>{date_label} · Issue #{issue_number}</p>"
        "<p>No relevant digital procurement updates cleared the relevance threshold this week.</p>"
        "<p>The pipeline completed successfully and will resume with the next scheduled run.</p>"
        "</body></html>"
    )
    plaintext = (
        "DIGITAL PROCUREMENT NEWS SCOUT\n"
        f"{date_label} · Issue #{issue_number}\n\n"
        "No relevant digital procurement updates cleared the relevance threshold this week.\n"
        "The pipeline completed successfully and will resume with the next scheduled run.\n"
    )
    return html, plaintext


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


def _resolve_issue_timezone(timezone_name: str | None) -> timezone | ZoneInfo:
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


def _validate_fetch_mode(*, ignore_seen_db: bool, reuse_seen_db: bool) -> None:
    if ignore_seen_db and reuse_seen_db:
        raise ValueError("ignore_seen_db and reuse_seen_db cannot both be enabled")


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
            parse_datetime(article.published_at or article.fetched_at or "") or datetime.min.replace(tzinfo=timezone.utc),
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


def _report_progress(
    progress_callback: Callable[[str], None] | None,
    message: str,
) -> None:
    emit_progress(progress_callback, message)


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def _partition_sources_by_category(
    sources: list[Any],
) -> tuple[list[Any], list[Any]]:
    procurement_sources: list[Any] = []
    global_news_sources: list[Any] = []
    for source in sources:
        category = str(getattr(source, "category", "")).strip().casefold()
        if category == GLOBAL_NEWS_CATEGORY:
            global_news_sources.append(source)
        else:
            procurement_sources.append(source)
    return procurement_sources, global_news_sources


def _partition_articles_by_category(
    articles: list[RawArticle],
) -> tuple[list[RawArticle], list[RawArticle]]:
    procurement_articles: list[RawArticle] = []
    global_news_articles: list[RawArticle] = []
    for article in articles:
        category = str(article.category).strip().casefold()
        if category == GLOBAL_NEWS_CATEGORY:
            global_news_articles.append(article)
        else:
            procurement_articles.append(article)
    return procurement_articles, global_news_articles


def _empty_fetch_summary(*, sources_attempted: int = 0, sources_failed: int = 0) -> FetchSummary:
    return FetchSummary(
        articles=[],
        sources_attempted=sources_attempted,
        sources_succeeded=0,
        sources_failed=sources_failed,
        articles_found=0,
        articles_deduplicated=0,
        articles_saved=0,
    )


def _merge_fetch_summaries(*summaries: FetchSummary) -> FetchSummary:
    merged_articles: list[RawArticle] = []
    article_index_by_url: dict[str, int] = {}

    for summary in summaries:
        for article in summary.articles:
            normalized = normalize_url(article.url)
            existing_index = article_index_by_url.get(normalized)
            if existing_index is None:
                article_index_by_url[normalized] = len(merged_articles)
                merged_articles.append(article)
                continue

            existing_article = merged_articles[existing_index]
            if _should_replace_merged_article(existing_article, article):
                merged_articles[existing_index] = article

    return FetchSummary(
        articles=merged_articles,
        sources_attempted=sum(summary.sources_attempted for summary in summaries),
        sources_succeeded=sum(summary.sources_succeeded for summary in summaries),
        sources_failed=sum(summary.sources_failed for summary in summaries),
        articles_found=sum(summary.articles_found for summary in summaries),
        articles_deduplicated=len(merged_articles),
        articles_saved=sum(summary.articles_saved for summary in summaries),
    )


def _should_replace_merged_article(existing: RawArticle, candidate: RawArticle) -> bool:
    existing_is_fallback = existing.discovery_method == "search_fallback"
    candidate_is_fallback = candidate.discovery_method == "search_fallback"

    if existing_is_fallback != candidate_is_fallback:
        return existing_is_fallback and not candidate_is_fallback

    if existing.category != GLOBAL_NEWS_CATEGORY and candidate.category == GLOBAL_NEWS_CATEGORY:
        return True

    return False


def _summary_has_total_fetch_outage(summary: FetchSummary | None) -> bool:
    return bool(summary and summary.total_fetch_outage)


def _prefixed_progress_callback(
    progress_callback: Callable[[str], None] | None,
    prefix: str,
) -> Callable[[str], None] | None:
    if progress_callback is None:
        return None
    return lambda message: _report_progress(progress_callback, f"{prefix}: {message}")


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


async def _run_global_fetch_stage(
    *,
    sources: list[Any],
    settings: Any,
    database_path: str,
    logger: Any,
    run_id: int,
    issue_number: int,
    now: datetime | None,
    ignore_seen_db: bool,
    progress_callback: Callable[[str], None] | None,
) -> FetchSummary:
    if not sources:
        return _empty_fetch_summary()

    try:
        return await fetch_all_sources_report(
            sources=sources,
            settings=settings,
            database_path=database_path,
            logger=get_logger(__name__, pipeline_stage="fetcher"),
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
        _report_progress(
            progress_callback,
            f"Global macro fetch failed; continuing without that section: {exc}",
        )
        return _empty_fetch_summary(
            sources_attempted=len(sources),
            sources_failed=len(sources),
        )


async def _run_global_scoring_stage(
    *,
    articles: list[RawArticle],
    settings: Any,
    logger: Any,
    run_id: int,
    issue_number: int,
    progress_callback: Callable[[str], None] | None,
    now: datetime | None,
) -> list[ScoredArticle]:
    if not articles:
        return []

    try:
        return await score_articles(
            articles,
            settings=settings,
            scoring_prompt_name="global_news_scoring.md",
            threshold=settings.global_news_relevance_threshold,
            logger=get_logger(__name__, pipeline_stage="analyzer"),
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
        _report_progress(
            progress_callback,
            f"Global macro scoring failed; continuing without that section: {exc}",
        )
        return []


async def _run_global_briefing_stage(
    *,
    articles: list[ScoredArticle],
    settings: Any,
    logger: Any,
    run_id: int,
    issue_number: int,
    progress_callback: Callable[[str], None] | None,
    now: datetime | None,
) -> list[Any]:
    if not articles:
        return []

    try:
        return await compose_global_briefing(
            articles,
            settings=settings,
            logger=get_logger(__name__, pipeline_stage="analyzer"),
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
        _report_progress(
            progress_callback,
            f"Global macro briefing composition failed; continuing without that section: {exc}",
        )
        return []


def main() -> None:
    config = load_config()
    configure_logging(config)
    progress_callback = (
        build_stdout_progress_callback()
        if _should_emit_stdout_progress()
        else None
    )
    if progress_callback is not None:
        emit_progress(
            progress_callback,
            "Starting pipeline run "
            f"(dry_run={config.settings.dry_run}).",
        )
        emit_progress(
            progress_callback,
            f"Using database {config.settings.database_path} and log file {config.settings.log_file}.",
        )
    result = run_pipeline(config=config, progress_callback=progress_callback)
    if result.status != "success":
        raise SystemExit(1)


def _should_emit_stdout_progress() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"


if __name__ == "__main__":
    main()
