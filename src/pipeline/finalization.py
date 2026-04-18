from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.storage.db import PipelineRunRecord, log_run, utc_now_iso
from src.utils.config import AppConfig


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


def finalize_pipeline_run(
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


def log_pipeline_stage_failure(
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


__all__ = [
    "PipelineResult",
    "finalize_pipeline_run",
    "log_pipeline_stage_failure",
]
