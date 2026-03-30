from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.storage.db import ArticleRecord, utc_now_iso


@dataclass(slots=True, frozen=True)
class Source:
    name: str
    url: str
    tier: int
    method: str
    active: bool
    category: str
    selectors: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawArticle:
    url: str
    title: str
    source: str
    source_url: str
    category: str
    published_at: str | None = None
    fetched_at: str = field(default_factory=utc_now_iso)
    summary: str | None = None
    author: str | None = None

    def to_record(self) -> ArticleRecord:
        return ArticleRecord(
            url=self.url,
            title=self.title,
            source=self.source,
            published_at=self.published_at,
            fetched_at=self.fetched_at,
            content_snippet=self.summary,
        )
