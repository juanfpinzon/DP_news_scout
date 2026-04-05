from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.analyzer.digest import Digest, compose_digest
from src.analyzer.relevance import ScoredArticle, score_articles
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
from src.utils.progress import emit_progress

DEFAULT_SUBJECT_PREFIX = "Digital Procurement News Scout"


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
    progress_callback: Callable[[str], None] | None = None,
) -> PipelineResult:
    _validate_fetch_mode(ignore_seen_db=ignore_seen_db, reuse_seen_db=reuse_seen_db)
    config = config or load_config()
    initialize_database(config.settings.database_path)
    logger = get_logger(__name__, pipeline_stage="pipeline")
    started_at = utc_now_iso()
    date_label = _format_display_date(now)
    default_subject = _build_digest_subject(
        issue_number=resolve_issue_number(config.settings, fallback=1),
        date_label=date_label,
    )

    run_id = log_run(
        config.settings.database_path,
        PipelineRunRecord(
            started_at=started_at,
            status="started",
            sources_fetched=0,
        ),
    )
    issue_number = resolve_issue_number(config.settings, fallback=run_id)
    default_subject = _build_digest_subject(issue_number=issue_number, date_label=date_label)

    try:
        sources = load_source_registry()
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
        f"Loaded {len(sources)} configured {_pluralize(len(sources), 'source')} for issue #{issue_number}.",
    )
    logger.info(
        "pipeline_started",
        run_id=run_id,
        issue_number=issue_number,
        source_count=len(sources),
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
                    sources=sources,
                    logger=logger,
                    now=now,
                    ignore_seen_db=ignore_seen_db,
                    reuse_seen_db=reuse_seen_db,
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
    sources: list[Any],
    logger: Any,
    now: datetime | None,
    ignore_seen_db: bool,
    reuse_seen_db: bool,
    progress_callback: Callable[[str], None] | None,
) -> PipelineResult:
    date_label = _format_display_date(now)
    subject = _build_digest_subject(issue_number=issue_number, date_label=date_label)
    fetch_summary = FetchSummary(
        articles=[],
        sources_attempted=len(sources),
        sources_succeeded=0,
        sources_failed=0,
        articles_found=0,
        articles_deduplicated=0,
        articles_saved=0,
    )
    raw_articles: list[RawArticle] = []
    scored_articles: list[ScoredArticle] = []
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
                sources=sources,
            )
            fetch_summary = FetchSummary(
                articles=raw_articles,
                sources_attempted=len(sources),
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
            )
            _report_progress(
                progress_callback,
                f"Loaded {len(raw_articles)} stored {_pluralize(len(raw_articles), 'article')} from SQLite.",
            )
        else:
            _report_progress(
                progress_callback,
                f"Fetching live articles from {len(sources)} configured {_pluralize(len(sources), 'source')}.",
            )
            fetch_summary = await fetch_all_sources_report(
                sources=sources,
                settings=config.settings,
                database_path=config.settings.database_path,
                logger=get_logger(__name__, pipeline_stage="fetcher"),
                now=now,
                persist_to_db=False,
                use_database_seen_urls=not ignore_seen_db,
                progress_callback=progress_callback,
            )
            raw_articles = fetch_summary.articles
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

    if not reuse_seen_db and fetch_summary.total_fetch_outage:
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
                f"{len(raw_articles)} {_pluralize(len(raw_articles), 'article')} for relevance "
                f"with {config.settings.llm_scoring_model}.",
            )
            scored_articles = await score_articles(
                raw_articles,
                settings=config.settings,
                logger=get_logger(__name__, pipeline_stage="analyzer"),
                progress_callback=progress_callback,
            )
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

    if scored_articles:
        try:
            _report_progress(
                progress_callback,
                "Composing digest from "
                f"{len(scored_articles)} relevant {_pluralize(len(scored_articles), 'article')} "
                f"with {config.settings.llm_digest_model}.",
            )
            digest = await compose_digest(
                scored_articles,
                settings=config.settings,
                logger=get_logger(__name__, pipeline_stage="analyzer"),
                progress_callback=progress_callback,
            )
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
                relevant_articles=len(scored_articles),
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
            "No articles cleared the relevance threshold. Building the no-news digest.",
        )
        html, plaintext = _build_no_news_email(issue_number=issue_number, date_label=date_label)
        subject = _build_no_news_subject(issue_number=issue_number, date_label=date_label)

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
        relevant_articles=len(scored_articles),
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
    }


def _build_digest_subject(*, issue_number: int, date_label: str) -> str:
    return f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | Issue #{issue_number}"


def _build_no_news_subject(*, issue_number: int, date_label: str) -> str:
    return f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | No major updates | Issue #{issue_number}"


def _build_no_news_email(*, issue_number: int, date_label: str) -> tuple[str, str]:
    html = (
        "<html><body>"
        "<h1>Digital Procurement News Scout</h1>"
        f"<p>{date_label} · Issue #{issue_number}</p>"
        "<p>No relevant digital procurement updates cleared the relevance threshold today.</p>"
        "<p>The pipeline completed successfully and will resume with the next scheduled run.</p>"
        "</body></html>"
    )
    plaintext = (
        "DIGITAL PROCUREMENT NEWS SCOUT\n"
        f"{date_label} · Issue #{issue_number}\n\n"
        "No relevant digital procurement updates cleared the relevance threshold today.\n"
        "The pipeline completed successfully and will resume with the next scheduled run.\n"
    )
    return html, plaintext


def _format_display_date(now: datetime | None = None) -> str:
    active_now = now or datetime.now(timezone.utc)
    return f"{active_now.strftime('%B')} {active_now.day}, {active_now.year}"


def resolve_issue_number(settings: Any, *, fallback: int) -> int:
    override = getattr(settings, "issue_number_override", None)
    if override is not None:
        return int(override)
    return fallback


def _validate_fetch_mode(*, ignore_seen_db: bool, reuse_seen_db: bool) -> None:
    if ignore_seen_db and reuse_seen_db:
        raise ValueError("ignore_seen_db and reuse_seen_db cannot both be enabled")


def load_raw_articles_from_storage(
    *,
    database_path: str,
    sources: list[Any],
) -> list[RawArticle]:
    stored_articles = load_articles(database_path)
    if not stored_articles:
        raise ValueError("no stored articles available in the database")
    source_lookup = {
        str(source.name).strip().casefold(): source
        for source in sources
    }
    return [
        _article_record_to_raw_article(record, source_lookup=source_lookup)
        for record in stored_articles
    ]


def _article_record_to_raw_article(
    record: ArticleRecord,
    *,
    source_lookup: dict[str, Any],
) -> RawArticle:
    source = source_lookup.get(record.source.strip().casefold())
    return RawArticle(
        url=record.url,
        title=record.title,
        source=record.source,
        source_url=source.url if source is not None else "",
        category=source.category if source is not None else "procurement",
        published_at=record.published_at,
        fetched_at=record.fetched_at or utc_now_iso(),
        summary=record.content_snippet,
        author=None,
    )


def _report_progress(
    progress_callback: Callable[[str], None] | None,
    message: str,
) -> None:
    emit_progress(progress_callback, message)


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def main() -> None:
    config = load_config()
    configure_logging(config)
    result = run_pipeline(config=config)
    if result.status != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
