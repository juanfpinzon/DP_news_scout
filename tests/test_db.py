from __future__ import annotations

import sqlite3
from datetime import datetime

from src.storage.db import (
    ArticleRecord,
    DeliveryRecord,
    PipelineRunRecord,
    get_recent_urls,
    initialize_database,
    load_articles,
    log_delivery,
    log_run,
    save_articles,
    utc_now_iso,
)


def test_database_helpers_round_trip(tmp_path) -> None:
    database_path = str(tmp_path / "dpns.db")

    inserted = save_articles(
        database_path,
        [
            ArticleRecord(
                url="https://example.com/story-1",
                title="Story 1",
                source="Example",
                published_at=utc_now_iso(),
                content_snippet="Snippet",
                origin_source="SAP Ariba",
                discovery_method="search_fallback",
                relevance_score=8,
                included_in_digest=True,
            )
        ],
    )
    assert inserted == 1
    assert "https://example.com/story-1" in get_recent_urls(database_path)

    run_id = log_run(
        database_path,
        PipelineRunRecord(
            started_at=utc_now_iso(),
            status="started",
            sources_fetched=2,
            articles_found=4,
            articles_included=1,
        ),
    )
    assert run_id > 0

    delivery_id = log_delivery(
        database_path,
        DeliveryRecord(
            run_id=run_id,
            sent_at=utc_now_iso(),
            recipient_count=3,
            status="sent",
        ),
    )
    assert delivery_id > 0

    with sqlite3.connect(database_path) as connection:
        article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        run_count = connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        delivery_count = connection.execute("SELECT COUNT(*) FROM delivery_log").fetchone()[0]

    assert article_count == 1
    assert run_count == 1
    assert delivery_count == 1

    articles = load_articles(database_path)
    assert articles[0].origin_source == "SAP Ariba"
    assert articles[0].discovery_method == "search_fallback"


def test_initialize_database_creates_file(tmp_path) -> None:
    database_path = tmp_path / "nested" / "dpns.db"

    initialize_database(str(database_path))

    assert database_path.exists()


def test_get_recent_urls_can_use_injected_now(tmp_path) -> None:
    database_path = str(tmp_path / "dpns.db")
    save_articles(
        database_path,
        [
            ArticleRecord(
                url="https://example.com/historical-story",
                title="Historical Story",
                source="Example",
                fetched_at="2026-03-31T09:00:00+00:00",
            ),
            ArticleRecord(
                url="https://example.com/future-story",
                title="Future Story",
                source="Example",
                fetched_at="2026-04-10T09:00:00+00:00",
            ),
        ],
    )

    assert get_recent_urls(
        database_path,
        days=7,
        now=datetime.fromisoformat("2026-04-04T09:00:00+00:00"),
    ) == {"https://example.com/historical-story"}
    assert get_recent_urls(
        database_path,
        days=7,
        now=datetime.fromisoformat("2026-04-17T09:00:00+00:00"),
    ) == {"https://example.com/future-story"}
