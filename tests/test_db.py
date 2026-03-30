from __future__ import annotations

import sqlite3

from src.storage.db import (
    ArticleRecord,
    DeliveryRecord,
    PipelineRunRecord,
    get_recent_urls,
    initialize_database,
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


def test_initialize_database_creates_file(tmp_path) -> None:
    database_path = tmp_path / "nested" / "dpns.db"

    initialize_database(str(database_path))

    assert database_path.exists()
