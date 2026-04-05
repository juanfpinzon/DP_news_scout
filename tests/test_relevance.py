from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.analyzer.relevance import (
    RelevanceScoringError,
    ScoredArticle,
    score_articles,
)
from src.fetcher.models import RawArticle
from src.utils.config import Settings


class DummyLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs) -> None:
        self.records.append((event, kwargs))


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
            }
        )
        return self.responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def build_settings() -> Settings:
    return Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Central European Time",
        llm_scoring_model="anthropic/claude-haiku-4.5",
        llm_digest_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-haiku-4.5",
        database_path="data/test.db",
        log_level="INFO",
        log_file="data/logs/test.jsonl",
        dry_run=True,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
    )


def build_article(index: int) -> RawArticle:
    return RawArticle(
        url=f"https://example.com/article-{index}",
        title=f"Article {index}",
        source="Example Source",
        source_url="https://example.com/feed.xml",
        category="trade_media",
        published_at="2026-04-04T08:00:00+00:00",
        summary=f"Summary for article {index}",
        author="Example Author",
    )


def test_score_articles_batches_filters_and_parses_fenced_json() -> None:
    articles = [build_article(index) for index in range(1, 12)]
    llm_client = FakeLLMClient(
        [
            """```json
            {
              "scores": [
                {"url": "https://example.com/article-1", "score": 9, "reasoning": "Core platform news."},
                {"url": "https://example.com/article-2", "score": 5, "reasoning": "Too broad."},
                {"url": "https://example.com/article-3", "score": 6, "reasoning": "Relevant AI trend."},
                {"url": "https://example.com/article-4", "score": 4, "reasoning": "Weakly related."},
                {"url": "https://example.com/article-5", "score": 10, "reasoning": "High-value procurement move."},
                {"url": "https://example.com/article-6", "score": 3, "reasoning": "Generic business news."},
                {"url": "https://example.com/article-7", "score": 7, "reasoning": "Useful competitive signal."},
                {"url": "https://example.com/article-8", "score": 6, "reasoning": "Relevant platform update."},
                {"url": "https://example.com/article-9", "score": 2, "reasoning": "Irrelevant."},
                {"url": "https://example.com/article-10", "score": 8, "reasoning": "Strongly relevant vendor news."}
              ]
            }
            ```""",
            """
            {
              "scores": [
                {"url": "https://example.com/article-11", "score": 7, "reasoning": "Relevant leadership strategy."}
              ]
            }
            """,
        ]
    )
    logger = DummyLogger()

    async def run() -> list[ScoredArticle]:
        return await score_articles(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            logger=logger,
        )

    scored_articles = asyncio.run(run())

    assert [article.url for article in scored_articles] == [
        "https://example.com/article-1",
        "https://example.com/article-3",
        "https://example.com/article-5",
        "https://example.com/article-7",
        "https://example.com/article-8",
        "https://example.com/article-10",
        "https://example.com/article-11",
    ]
    assert scored_articles[0].relevance_score == 9
    assert scored_articles[0].reasoning == "Core platform news."
    assert scored_articles[0].title == "Article 1"
    assert len(llm_client.calls) == 2
    assert llm_client.calls[0]["max_tokens"] == 1600
    assert "PepsiCo" in str(llm_client.calls[0]["system_prompt"])
    assert "Return strict JSON only" in str(llm_client.calls[0]["system_prompt"])
    assert "https://example.com/article-10" in str(llm_client.calls[0]["user_prompt"])
    assert "https://example.com/article-11" in str(llm_client.calls[1]["user_prompt"])
    assert logger.records[-1][0] == "relevance_scoring_complete"


def test_score_articles_raises_on_missing_scores() -> None:
    articles = [build_article(1), build_article(2)]
    llm_client = FakeLLMClient(
        [
            """
            {
              "scores": [
                {"url": "https://example.com/article-1", "score": 8, "reasoning": "Relevant."}
              ]
            }
            """
        ]
    )

    async def run() -> None:
        await score_articles(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
        )

    with pytest.raises(RelevanceScoringError, match="Missing relevance scores"):
        asyncio.run(run())


def test_scored_article_to_record_sets_relevance_score() -> None:
    article = build_article(1)
    scored = ScoredArticle(
        url=article.url,
        title=article.title,
        source=article.source,
        source_url=article.source_url,
        category=article.category,
        published_at=article.published_at,
        fetched_at=article.fetched_at,
        summary=article.summary,
        author=article.author,
        relevance_score=8,
        reasoning="Relevant platform update.",
    )

    record = scored.to_record()

    assert record.url == article.url
    assert record.content_snippet == article.summary
    assert record.relevance_score == 8.0


def test_score_articles_rejects_duplicate_urls_in_batch() -> None:
    article = build_article(1)
    duplicate = replace(article)
    llm_client = FakeLLMClient([])

    async def run() -> None:
        await score_articles(
            [article, duplicate],
            llm_client=llm_client,
            settings=build_settings(),
        )

    with pytest.raises(RelevanceScoringError, match="unique URL"):
        asyncio.run(run())


def test_score_articles_uses_scoring_model_when_instantiating_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingLLMClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
            return """
            {
              "scores": [
                {"url": "https://example.com/article-1", "score": 8, "reasoning": "Relevant."}
              ]
            }
            """

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("src.analyzer.relevance.LLMClient", CapturingLLMClient)

    async def run() -> list[ScoredArticle]:
        return await score_articles([build_article(1)], settings=build_settings())

    scored_articles = asyncio.run(run())

    assert [article.url for article in scored_articles] == ["https://example.com/article-1"]
    assert captured["primary_model"] == "anthropic/claude-haiku-4.5"


def test_score_articles_uses_scoring_model_when_supplied_client_supports_override() -> None:
    class OverrideableLLMClient(FakeLLMClient):
        def __init__(self, responses: list[str]) -> None:
            super().__init__(responses)
            self.requested_primary_models: list[str] = []

        def with_primary_model(self, primary_model: str) -> OverrideableLLMClient:
            self.requested_primary_models.append(primary_model)
            return self

    llm_client = OverrideableLLMClient(
        [
            """
            {
              "scores": [
                {"url": "https://example.com/article-1", "score": 8, "reasoning": "Relevant."}
              ]
            }
            """
        ]
    )

    async def run() -> list[ScoredArticle]:
        return await score_articles(
            [build_article(1)],
            llm_client=llm_client,
            settings=build_settings(),
        )

    scored_articles = asyncio.run(run())

    assert [article.url for article in scored_articles] == ["https://example.com/article-1"]
    assert llm_client.requested_primary_models == ["anthropic/claude-haiku-4.5"]
