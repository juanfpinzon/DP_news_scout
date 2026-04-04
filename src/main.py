from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.analyzer.digest import Digest, compose_digest
from src.analyzer.relevance import ScoredArticle, score_articles
from src.fetcher import FetchSummary, RawArticle, fetch_all_sources_report, load_source_registry
from src.renderer import render_digest, render_plaintext
from src.sender import send_digest
from src.storage.db import PipelineRunRecord, initialize_database, log_run, save_articles, utc_now_iso
from src.utils.config import AppConfig, load_config
from src.utils.logging import configure_logging, get_logger

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
) -> PipelineResult:
    config = config or load_config()
    initialize_database(config.settings.database_path)
    logger = get_logger(__name__, pipeline_stage="pipeline")
    started_at = utc_now_iso()
    sources = load_source_registry()

    run_id = log_run(
        config.settings.database_path,
        PipelineRunRecord(
            started_at=started_at,
            status="started",
            sources_fetched=0,
        ),
    )
    issue_number = run_id

    logger.info(
        "pipeline_started",
        run_id=run_id,
        issue_number=issue_number,
        source_count=len(sources),
        recipient_group=config.default_recipient_group,
        recipient_count=len(config.recipients),
        dry_run=config.settings.dry_run,
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
                ),
                timeout=config.settings.pipeline_timeout,
            )
        )
    except asyncio.TimeoutError:
        error = (
            f"Pipeline timed out after {config.settings.pipeline_timeout} seconds"
        )
        completed_at = utc_now_iso()
        log_run(
            config.settings.database_path,
            PipelineRunRecord(
                started_at=started_at,
                completed_at=completed_at,
                status="failed",
                sources_fetched=0,
                error_log=error,
            ),
            run_id=run_id,
        )
        logger.error(
            "pipeline_timed_out",
            run_id=run_id,
            issue_number=issue_number,
            timeout_seconds=config.settings.pipeline_timeout,
        )
        return PipelineResult(
            run_id=run_id,
            issue_number=issue_number,
            status="failed",
            started_at=started_at,
            completed_at=completed_at,
            sources_fetched=0,
            articles_found=0,
            relevant_articles=0,
            articles_included=0,
            email_sent=False,
            subject=_build_digest_subject(issue_number=issue_number, date_label=_format_display_date(now)),
            dry_run=config.settings.dry_run,
            error=error,
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
        fetch_summary = await fetch_all_sources_report(
            sources=sources,
            settings=config.settings,
            database_path=config.settings.database_path,
            logger=get_logger(__name__, pipeline_stage="fetcher"),
            now=now,
            persist_to_db=False,
        )
        raw_articles = fetch_summary.articles
    except Exception as exc:
        status = "failed"
        error = f"fetcher stage failed: {exc}"
        logger.error(
            "pipeline_stage_failed",
            run_id=run_id,
            issue_number=issue_number,
            stage="fetcher",
            error=str(exc),
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

    if fetch_summary.total_fetch_outage:
        status = "failed"
        error = "fetcher stage failed: all configured sources failed to fetch"
        logger.error(
            "pipeline_stage_failed",
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
            scored_articles = await score_articles(
                raw_articles,
                settings=config.settings,
                logger=get_logger(__name__, pipeline_stage="analyzer"),
            )
        except Exception as exc:
            status = "failed"
            error = f"analyzer relevance stage failed: {exc}"
            logger.error(
                "pipeline_stage_failed",
                run_id=run_id,
                issue_number=issue_number,
                stage="analyzer_relevance",
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
            digest = await compose_digest(
                scored_articles,
                settings=config.settings,
                logger=get_logger(__name__, pipeline_stage="analyzer"),
            )
        except Exception as exc:
            status = "failed"
            error = f"digest composition stage failed: {exc}"
            logger.error(
                "pipeline_stage_failed",
                run_id=run_id,
                issue_number=issue_number,
                stage="digest_composition",
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
                articles_included=0,
                email_sent=False,
                dry_run=config.settings.dry_run,
                status=status,
                error=error,
                logger=logger,
            )

        try:
            html = render_digest(digest, issue_number=issue_number, date=date_label)
            plaintext = render_plaintext(digest, issue_number=issue_number, date=date_label)
        except Exception as exc:
            status = "failed"
            error = f"renderer stage failed: {exc}"
            logger.error(
                "pipeline_stage_failed",
                run_id=run_id,
                issue_number=issue_number,
                stage="renderer",
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
                articles_included=0,
                email_sent=False,
                dry_run=config.settings.dry_run,
                status=status,
                error=error,
                logger=logger,
            )

        articles_included = _count_digest_articles(digest)
    else:
        html, plaintext = _build_no_news_email(issue_number=issue_number, date_label=date_label)
        subject = _build_no_news_subject(issue_number=issue_number, date_label=date_label)

    if config.settings.dry_run:
        logger.info(
            "pipeline_send_skipped",
            run_id=run_id,
            issue_number=issue_number,
            reason="dry_run",
            subject=subject,
        )
    else:
        try:
            email_sent = send_digest(
                html,
                plaintext,
                subject,
                config=config,
                run_id=run_id,
            )
        except Exception as exc:
            status = "failed"
            error = f"sender stage failed: {exc}"
            logger.error(
                "pipeline_stage_failed",
                run_id=run_id,
                issue_number=issue_number,
                stage="sender",
                error=str(exc),
            )
        else:
            if not email_sent:
                status = "failed"
                error = "sender stage returned unsuccessful delivery status"

    if status == "success" and email_sent:
        try:
            _persist_seen_articles(
                database_path=config.settings.database_path,
                raw_articles=raw_articles,
                scored_articles=scored_articles,
                digest=digest,
                logger=logger,
                run_id=run_id,
                issue_number=issue_number,
            )
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


def main() -> None:
    config = load_config()
    configure_logging(config)
    result = run_pipeline(config=config)
    if result.status != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
