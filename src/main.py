from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from src.analyzer.digest import Digest, compose_digest
from src.analyzer.global_briefing import compose_global_briefing
from src.analyzer.relevance import ScoredArticle, score_articles
from src.fetcher.common import DomainRateLimiter, RobotsPolicy, managed_async_client
from src.fetcher import FetchSummary, RawArticle, fetch_all_sources_report, load_source_registry
from src.pipeline.finalization import (
    PipelineResult,
    finalize_pipeline_run as _finalize_pipeline_run,
    log_pipeline_stage_failure as _log_pipeline_stage_failure,
)
from src.pipeline.global_stages import (
    run_global_briefing_stage,
    run_global_fetch_stage,
    run_global_scoring_stage,
)
from src.pipeline.issue_number import format_display_date, resolve_issue_number
from src.pipeline.partition import (
    empty_fetch_summary,
    merge_fetch_summaries,
    partition_articles_by_category,
    partition_sources_by_category,
    summary_has_total_fetch_outage,
)
from src.pipeline.persistence import (
    count_digest_articles,
    persist_seen_articles,
)
from src.pipeline.reuse import load_raw_articles_from_storage
from src.pipeline.subject import (
    DEFAULT_SUBJECT_PREFIX,
    build_digest_subject,
    build_no_news_subject,
)
from src.renderer import render_digest, render_plaintext
from src.sender import send_digest
from src.storage.db import (
    PipelineRunRecord,
    initialize_database,
    log_run,
    utc_now_iso,
)
from src.utils.config import AppConfig, load_config
from src.utils.logging import configure_logging, get_logger
from src.utils.progress import build_stdout_progress_callback, emit_progress


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
    default_subject = build_digest_subject(
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
    default_subject = build_digest_subject(
        issue_number=issue_number,
        date_label=date_label,
        subject_suffix=subject_suffix,
    )

    try:
        sources = load_source_registry(
            include_fallback_only=config.settings.search_fallback_enabled,
        )
        procurement_sources, global_news_sources = partition_sources_by_category(sources)
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
            status="timeout",
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
    subject = build_digest_subject(
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
    procurement_fetch_summary = empty_fetch_summary(
        sources_attempted=len(procurement_sources),
    )
    global_fetch_summary = empty_fetch_summary(
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
            procurement_raw_articles, global_raw_articles = partition_articles_by_category(raw_articles)
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
            shared_rate_limiter = DomainRateLimiter(config.settings.rate_limit_seconds)
            shared_robots_policy = RobotsPolicy()
            async with managed_async_client(
                None,
                timeout_seconds=config.settings.request_timeout_seconds,
            ) as shared_client:
                procurement_fetch_summary, global_fetch_summary = await asyncio.gather(
                    fetch_all_sources_report(
                        sources=procurement_sources,
                        settings=config.settings,
                        database_path=config.settings.database_path,
                        logger=get_logger(__name__, pipeline_stage="fetcher"),
                        client=shared_client,
                        rate_limiter=shared_rate_limiter,
                        robots_policy=shared_robots_policy,
                        allow_robots_network_fallback=True,
                        now=now,
                        persist_to_db=False,
                        use_database_seen_urls=not ignore_seen_db,
                        progress_callback=procurement_progress,
                    ),
                    run_global_fetch_stage(
                        sources=global_news_sources,
                        settings=config.settings,
                        database_path=config.settings.database_path,
                        logger=logger,
                        run_id=run_id,
                        issue_number=issue_number,
                        client=shared_client,
                        rate_limiter=shared_rate_limiter,
                        robots_policy=shared_robots_policy,
                        now=now,
                        ignore_seen_db=ignore_seen_db,
                        progress_callback=global_progress,
                        fetch_all_sources_report_fn=fetch_all_sources_report,
                        logger_factory=get_logger,
                    ),
                )
            fetch_summary = merge_fetch_summaries(
                procurement_fetch_summary,
                global_fetch_summary,
            )
            raw_articles = fetch_summary.articles
            procurement_raw_articles, global_raw_articles = partition_articles_by_category(raw_articles)
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

    if not reuse_seen_db and procurement_sources and summary_has_total_fetch_outage(procurement_fetch_summary):
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
                run_global_scoring_stage(
                    articles=global_raw_articles,
                    settings=config.settings,
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    progress_callback=global_progress,
                    now=now,
                    score_articles_fn=score_articles,
                    logger_factory=get_logger,
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
                run_global_briefing_stage(
                    articles=global_scored_articles,
                    settings=config.settings,
                    logger=logger,
                    run_id=run_id,
                    issue_number=issue_number,
                    progress_callback=global_progress,
                    now=now,
                    compose_global_briefing_fn=compose_global_briefing,
                    logger_factory=get_logger,
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

        articles_included = count_digest_articles(digest)
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
        subject = build_no_news_subject(
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
            persist_seen_articles(
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


def _report_progress(
    progress_callback: Callable[[str], None] | None,
    message: str,
) -> None:
    emit_progress(progress_callback, message)


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def _validate_fetch_mode(*, ignore_seen_db: bool, reuse_seen_db: bool) -> None:
    if ignore_seen_db and reuse_seen_db:
        raise ValueError("ignore_seen_db and reuse_seen_db cannot both be enabled")


def _prefixed_progress_callback(
    progress_callback: Callable[[str], None] | None,
    prefix: str,
) -> Callable[[str], None] | None:
    if progress_callback is None:
        return None
    return lambda message: _report_progress(progress_callback, f"{prefix}: {message}")
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
