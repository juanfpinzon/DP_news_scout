from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ArticleRecord:
    url: str
    title: str
    source: str
    published_at: str | None = None
    fetched_at: str | None = None
    content_snippet: str | None = None
    relevance_score: float | None = None
    included_in_digest: bool = False


@dataclass(slots=True)
class PipelineRunRecord:
    started_at: str
    completed_at: str | None = None
    status: str = "started"
    sources_fetched: int = 0
    articles_found: int = 0
    articles_included: int = 0
    error_log: str | None = None


@dataclass(slots=True)
class DeliveryRecord:
    run_id: int
    sent_at: str
    recipient_count: int
    status: str
    error: str | None = None


def initialize_database(database_path: str) -> None:
    with _connect_database(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                published_at TEXT,
                fetched_at TEXT,
                content_snippet TEXT,
                relevance_score REAL,
                included_in_digest INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                sources_fetched INTEGER NOT NULL DEFAULT 0,
                articles_found INTEGER NOT NULL DEFAULT 0,
                articles_included INTEGER NOT NULL DEFAULT 0,
                error_log TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                recipient_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles (published_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_log_run_id ON delivery_log (run_id)"
        )
        connection.commit()


def save_articles(database_path: str, articles: list[ArticleRecord | dict[str, Any]]) -> int:
    initialize_database(database_path)
    normalized_articles = [_normalize_article(article) for article in articles]
    if not normalized_articles:
        return 0

    field_names = [field.name for field in fields(ArticleRecord)]
    rows = [tuple(asdict(article)[name] for name in field_names) for article in normalized_articles]
    placeholders = ", ".join("?" for _ in field_names)

    with _connect_database(database_path) as connection:
        with closing(connection.cursor()) as cursor:
            cursor.executemany(
                f"""
                INSERT INTO articles ({", ".join(field_names)})
                VALUES ({placeholders})
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    source=excluded.source,
                    published_at=excluded.published_at,
                    fetched_at=excluded.fetched_at,
                    content_snippet=excluded.content_snippet,
                    relevance_score=excluded.relevance_score,
                    included_in_digest=excluded.included_in_digest
                """,
                rows,
            )
        connection.commit()
    return len(normalized_articles)


def get_recent_urls(database_path: str, days: int = 2) -> set[str]:
    initialize_database(database_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect_database(database_path) as connection:
        rows = connection.execute(
            "SELECT url FROM articles WHERE COALESCE(fetched_at, published_at, '') >= ?",
            (cutoff,),
        ).fetchall()
    return {row[0] for row in rows}


def log_run(
    database_path: str,
    run: PipelineRunRecord | dict[str, Any],
    run_id: int | None = None,
) -> int:
    initialize_database(database_path)
    run_record = _normalize_run(run)
    payload = asdict(run_record)

    with _connect_database(database_path) as connection:
        if run_id is None:
            cursor = connection.execute(
                """
                INSERT INTO pipeline_runs (
                    started_at,
                    completed_at,
                    status,
                    sources_fetched,
                    articles_found,
                    articles_included,
                    error_log
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["started_at"],
                    payload["completed_at"],
                    payload["status"],
                    payload["sources_fetched"],
                    payload["articles_found"],
                    payload["articles_included"],
                    payload["error_log"],
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

        connection.execute(
            """
            UPDATE pipeline_runs
            SET completed_at = ?,
                status = ?,
                sources_fetched = ?,
                articles_found = ?,
                articles_included = ?,
                error_log = ?
            WHERE id = ?
            """,
            (
                payload["completed_at"],
                payload["status"],
                payload["sources_fetched"],
                payload["articles_found"],
                payload["articles_included"],
                payload["error_log"],
                run_id,
            ),
        )
        connection.commit()
        return run_id


def log_delivery(database_path: str, delivery: DeliveryRecord | dict[str, Any]) -> int:
    initialize_database(database_path)
    delivery_record = _normalize_delivery(delivery)
    payload = asdict(delivery_record)
    with _connect_database(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO delivery_log (run_id, sent_at, recipient_count, status, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload["run_id"],
                payload["sent_at"],
                payload["recipient_count"],
                payload["status"],
                payload["error"],
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_database(database_path: str) -> sqlite3.Connection:
    db_path = Path(database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _normalize_article(article: ArticleRecord | dict[str, Any]) -> ArticleRecord:
    if isinstance(article, ArticleRecord):
        return article

    data = dict(article)
    data.setdefault("fetched_at", utc_now_iso())
    return ArticleRecord(**data)


def _normalize_run(run: PipelineRunRecord | dict[str, Any]) -> PipelineRunRecord:
    if isinstance(run, PipelineRunRecord):
        return run
    return PipelineRunRecord(**run)


def _normalize_delivery(delivery: DeliveryRecord | dict[str, Any]) -> DeliveryRecord:
    if isinstance(delivery, DeliveryRecord):
        return delivery
    return DeliveryRecord(**delivery)
