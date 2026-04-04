from __future__ import annotations

import argparse
import asyncio
import sqlite3
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from src.analyzer.digest import compose_digest
from src.analyzer.llm_client import LLMClient
from src.analyzer.relevance import score_articles
from src.fetcher import fetch_all_sources_report, load_source_registry
from src.main import DEFAULT_SUBJECT_PREFIX, PipelineResult, run_pipeline
from src.renderer import render_digest, render_plaintext
from src.sender import send_digest
from src.storage.db import initialize_database
from src.utils.config import DEFAULT_ENV_FILE, AppConfig, RecipientConfig, load_config
from src.utils.logging import configure_logging, get_logger

DEFAULT_PREVIEW_PATH = Path("/tmp/preview.html")
MANUAL_TEST_GROUP = "manual_test"


@dataclass(slots=True)
class RenderedDigestResult:
    issue_number: int
    date_label: str
    subject: str
    html: str
    plaintext: str
    raw_article_count: int
    relevant_article_count: int
    included_article_count: int
    no_news: bool


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Digital Procurement News Scout pipeline manually.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline without sending the digest.",
    )
    parser.add_argument(
        "--test-email",
        metavar="EMAIL",
        help="Render the live digest and send it only to this address.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Render the live digest, save an HTML preview, and open it in a browser.",
    )
    parser.add_argument(
        "--preview-path",
        default=str(DEFAULT_PREVIEW_PATH),
        help="Where to save the HTML preview when using --preview.",
    )
    parser.add_argument(
        "--plaintext-path",
        help="Optional path for the plain-text preview when using --preview.",
    )
    parser.add_argument(
        "--sources-only",
        action="store_true",
        help="Fetch sources only and report article counts without LLM analysis or email.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(DEFAULT_ENV_FILE)
    args = parse_args(argv)
    _validate_args(args)

    config = _load_runtime_config(dry_run_override=args.dry_run if _uses_pipeline_mode(args) else None)
    configure_logging(config)

    try:
        if args.sources_only:
            return _run_sources_only(config=config, now=_current_time())
        if args.preview or args.test_email:
            return _run_render_mode(
                config=config,
                preview=args.preview,
                preview_path=Path(args.preview_path).expanduser(),
                plaintext_path=(
                    Path(args.plaintext_path).expanduser()
                    if args.plaintext_path
                    else None
                ),
                test_recipient=args.test_email,
                now=_current_time(),
            )

        result = run_pipeline(config=config, now=_current_time())
    except Exception as exc:
        print(f"Manual run failed: {exc}")
        return 1

    _print_pipeline_summary(result)
    return 0 if result.status == "success" else 1


def _validate_args(args: argparse.Namespace) -> None:
    if args.sources_only and (args.dry_run or args.preview or args.test_email):
        raise SystemExit(
            "--sources-only cannot be combined with --dry-run, --preview, or --test-email",
        )
    if args.dry_run and (args.preview or args.test_email):
        raise SystemExit("--dry-run cannot be combined with --preview or --test-email")


def _uses_pipeline_mode(args: argparse.Namespace) -> bool:
    return not args.sources_only and not args.preview and not args.test_email


def _load_runtime_config(*, dry_run_override: bool | None) -> AppConfig:
    config = load_config()
    if dry_run_override is None or config.settings.dry_run == dry_run_override:
        return config

    return replace(
        config,
        settings=replace(config.settings, dry_run=dry_run_override),
    )


def _run_sources_only(*, config: AppConfig, now: datetime) -> int:
    logger = get_logger(__name__, pipeline_stage="manual")
    sources = load_source_registry()
    fetch_summary = asyncio.run(
        fetch_all_sources_report(
            sources=sources,
            settings=config.settings,
            database_path=config.settings.database_path,
            logger=get_logger(__name__, pipeline_stage="fetcher"),
            now=now,
            persist_to_db=False,
        )
    )
    raw_articles = fetch_summary.articles

    if fetch_summary.total_fetch_outage:
        print("Manual fetch failed: all configured sources failed to fetch.")
        return 1

    logger.info(
        "manual_sources_only_complete",
        sources_attempted=fetch_summary.sources_attempted,
        sources_succeeded=fetch_summary.sources_succeeded,
        sources_failed=fetch_summary.sources_failed,
        articles_found=len(raw_articles),
    )
    print(
        f"Fetched {len(raw_articles)} deduplicated articles from {len(sources)} configured sources.",
    )
    return 0


def _run_render_mode(
    *,
    config: AppConfig,
    preview: bool,
    preview_path: Path,
    plaintext_path: Path | None,
    test_recipient: str | None,
    now: datetime,
) -> int:
    rendered = asyncio.run(_render_live_digest(config=config, now=now))

    print(
        f"Rendered digest for issue #{rendered.issue_number}: "
        f"{rendered.raw_article_count} fetched, "
        f"{rendered.relevant_article_count} relevant, "
        f"{rendered.included_article_count} included.",
    )

    if preview:
        resolved_plaintext_path = plaintext_path or _default_plaintext_path(preview_path)
        _save_preview(rendered.html, preview_path)
        _save_preview(rendered.plaintext, resolved_plaintext_path)
        print(f"HTML preview saved to {preview_path}")
        print(f"Plain-text preview saved to {resolved_plaintext_path}")
        _open_preview_in_browser(preview_path)

    if test_recipient:
        test_config = _build_test_email_config(config, recipient=test_recipient)
        sent = send_digest(
            rendered.html,
            rendered.plaintext,
            rendered.subject,
            config=test_config,
        )
        if not sent:
            print(f"Test email delivery failed for {test_recipient}")
            return 1
        print(f"Sent test digest to {test_recipient}")

    return 0


async def _render_live_digest(
    *,
    config: AppConfig,
    now: datetime,
) -> RenderedDigestResult:
    initialize_database(config.settings.database_path)
    date_label = _format_display_date(now)
    issue_number = _next_issue_number(config.settings.database_path)
    sources = load_source_registry()

    fetch_summary = await fetch_all_sources_report(
        sources=sources,
        settings=config.settings,
        database_path=config.settings.database_path,
        logger=get_logger(__name__, pipeline_stage="fetcher"),
        now=now,
        persist_to_db=False,
    )
    raw_articles = fetch_summary.articles
    if fetch_summary.total_fetch_outage:
        raise RuntimeError("all configured sources failed to fetch")
    if not raw_articles:
        subject = _build_no_news_subject(issue_number=issue_number, date_label=date_label)
        html, plaintext = _build_no_news_email(issue_number=issue_number, date_label=date_label)
        return RenderedDigestResult(
            issue_number=issue_number,
            date_label=date_label,
            subject=subject,
            html=html,
            plaintext=plaintext,
            raw_article_count=0,
            relevant_article_count=0,
            included_article_count=0,
            no_news=True,
        )

    async with LLMClient(app_config=config, settings=config.settings) as llm_client:
        scored_articles = await score_articles(
            raw_articles,
            llm_client=llm_client,
            settings=config.settings,
            logger=get_logger(__name__, pipeline_stage="analyzer"),
        )

        if not scored_articles:
            subject = _build_no_news_subject(issue_number=issue_number, date_label=date_label)
            html, plaintext = _build_no_news_email(issue_number=issue_number, date_label=date_label)
            return RenderedDigestResult(
                issue_number=issue_number,
                date_label=date_label,
                subject=subject,
                html=html,
                plaintext=plaintext,
                raw_article_count=len(raw_articles),
                relevant_article_count=0,
                included_article_count=0,
                no_news=True,
            )

        digest = await compose_digest(
            scored_articles,
            llm_client=llm_client,
            settings=config.settings,
            logger=get_logger(__name__, pipeline_stage="analyzer"),
        )

    html = render_digest(digest, issue_number=issue_number, date=date_label)
    plaintext = render_plaintext(digest, issue_number=issue_number, date=date_label)
    return RenderedDigestResult(
        issue_number=issue_number,
        date_label=date_label,
        subject=_build_digest_subject(issue_number=issue_number, date_label=date_label),
        html=html,
        plaintext=plaintext,
        raw_article_count=len(raw_articles),
        relevant_article_count=len(scored_articles),
        included_article_count=_count_digest_articles(digest),
        no_news=False,
    )


def _build_test_email_config(config: AppConfig, *, recipient: str) -> AppConfig:
    normalized_recipient = RecipientConfig(email=recipient.strip())
    recipients = [normalized_recipient]
    return replace(
        config,
        recipients=recipients,
        recipient_groups={MANUAL_TEST_GROUP: recipients},
        default_recipient_group=MANUAL_TEST_GROUP,
    )


def _save_preview(content: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _open_preview_in_browser(preview_path: Path) -> None:
    try:
        opened = webbrowser.open(preview_path.resolve().as_uri())
    except Exception:
        print("Could not open the preview automatically; open the saved file manually.")
        return

    if not opened:
        print("Could not open the preview automatically; open the saved file manually.")


def _print_pipeline_summary(result: PipelineResult) -> None:
    if result.status == "success":
        mode_label = "Dry-run" if result.dry_run else "Pipeline"
        print(
            f"{mode_label} completed: issue #{result.issue_number}, "
            f"{result.articles_found} fetched, "
            f"{result.relevant_articles} relevant, "
            f"{result.articles_included} included, "
            f"email_sent={result.email_sent}.",
        )
        return

    print(
        f"Pipeline failed: issue #{result.issue_number}, "
        f"status={result.status}, "
        f"error={result.error or 'unknown error'}.",
    )


def _next_issue_number(database_path: str) -> int:
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()
    return int(row[0]) + 1 if row else 1


def _count_digest_articles(digest) -> int:
    return (
        1
        + len(digest.key_developments)
        + len(digest.on_our_radar)
        + len(digest.quick_hits)
    )


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


def _default_plaintext_path(preview_path: Path) -> Path:
    if preview_path.suffix:
        return preview_path.with_suffix(".txt")
    return Path(f"{preview_path}.txt")


def _format_display_date(current_time: datetime) -> str:
    return current_time.strftime("%B ") + str(current_time.day) + current_time.strftime(", %Y")


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
