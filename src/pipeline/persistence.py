from __future__ import annotations

from typing import Any

from src.analyzer.digest import Digest
from src.analyzer.relevance import ScoredArticle
from src.fetcher.models import RawArticle
from src.storage.db import save_articles


def count_digest_articles(digest: Digest) -> int:
    return (
        1
        + len(digest.key_developments)
        + len(digest.on_our_radar)
        + len(digest.quick_hits)
        + len(digest.global_briefing)
    )


def persist_seen_articles(
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
    included_urls = collect_included_urls(digest) if digest is not None else set()
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


def collect_included_urls(digest: Digest) -> set[str]:
    return {
        digest.top_story.url,
        *(item.url for item in digest.key_developments),
        *(item.url for item in digest.on_our_radar),
        *(item.url for item in digest.quick_hits),
        *(item.url for item in digest.global_briefing),
    }


__all__ = [
    "collect_included_urls",
    "count_digest_articles",
    "persist_seen_articles",
]
