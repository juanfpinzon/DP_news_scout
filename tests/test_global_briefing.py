from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.analyzer.digest import DigestItem
from src.analyzer.global_briefing import (
    GlobalBriefingCompositionError,
    compose_global_briefing,
)
from src.analyzer.relevance import ScoredArticle
from src.utils.config import Settings


class DummyLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs) -> None:
        self.records.append((event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.records.append((event, kwargs))


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        response_format: dict[str, object] | None = None,
        extra_body: dict[str, object] | None = None,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "extra_body": extra_body,
            }
        )
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None


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
        global_news_relevance_threshold=5,
        global_news_max_items=3,
        global_news_max_per_source=2,
    )


def build_article(index: int, *, source: str = "Reuters", score: int = 8) -> ScoredArticle:
    return ScoredArticle(
        url=f"https://example.com/article-{index}",
        title=f"Macro Article {index}",
        source=source,
        source_url="https://example.com/feed.xml",
        category="global_news",
        published_at=f"2026-04-0{index}T08:00:00+00:00",
        summary=f"Summary for macro article {index}",
        author="Example Author",
        relevance_score=score,
        reasoning=f"Reasoning for macro article {index}",
    )


def test_compose_global_briefing_parses_valid_payload() -> None:
    articles = [
        build_article(1, source="Reuters", score=9),
        build_article(2, source="BBC", score=8),
        build_article(3, source="CNN", score=7),
    ]
    llm_client = FakeLLMClient(
        [
            """
            {
              "global_briefing": [
                {
                  "url": "https://example.com/article-1",
                  "headline": "Tariff escalation hits sourcing outlook",
                  "summary": "Macro summary one.",
                  "why_it_matters": "Procurement implication one.",
                  "source": "Reuters",
                  "date": "2026-04-01"
                },
                {
                  "url": "https://example.com/article-2",
                  "headline": "Shipping route disruption raises freight risk",
                  "summary": "Macro summary two.",
                  "why_it_matters": "Procurement implication two.",
                  "source": "BBC",
                  "date": "2026-04-02"
                }
              ]
            }
            """
        ]
    )
    logger = DummyLogger()

    async def run() -> list[DigestItem]:
        return await compose_global_briefing(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            logger=logger,
        )

    global_briefing = asyncio.run(run())

    assert [item.url for item in global_briefing] == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]
    assert global_briefing[0].headline == "Tariff escalation hits sourcing outlook"
    assert llm_client.calls[0]["response_format"] == {"type": "json_object"}
    assert llm_client.calls[0]["extra_body"] == {"plugins": [{"id": "response-healing"}]}
    assert "trusted geopolitical analyst briefing" in str(llm_client.calls[0]["system_prompt"]).lower()
    assert "global macro briefing" in str(llm_client.calls[0]["user_prompt"]).lower()
    assert logger.records[-1][0] == "global_briefing_composition_complete"


def test_compose_global_briefing_rejects_unknown_urls() -> None:
    llm_client = FakeLLMClient(
        [
            """
            {
              "global_briefing": [
                {
                  "url": "https://example.com/article-999",
                  "headline": "Unknown item",
                  "summary": "Summary.",
                  "why_it_matters": "Implication.",
                  "source": "Reuters",
                  "date": "2026-04-01"
                }
              ]
            }
            """,
            """
            {
              "global_briefing": [
                {
                  "url": "https://example.com/article-999",
                  "headline": "Unknown item",
                  "summary": "Summary.",
                  "why_it_matters": "Implication.",
                  "source": "Reuters",
                  "date": "2026-04-01"
                }
              ]
            }
            """,
            """
            {
              "global_briefing": [
                {
                  "url": "https://example.com/article-999",
                  "headline": "Unknown item",
                  "summary": "Summary.",
                  "why_it_matters": "Implication.",
                  "source": "Reuters",
                  "date": "2026-04-01"
                }
              ]
            }
            """,
        ]
    )

    async def run() -> None:
        await compose_global_briefing(
            [build_article(1)],
            llm_client=llm_client,
            settings=build_settings(),
        )

    with pytest.raises(GlobalBriefingCompositionError, match="unknown article URL"):
        asyncio.run(run())


def test_compose_global_briefing_respects_global_source_cap() -> None:
    settings = replace(
        build_settings(),
        global_news_max_items=2,
        global_news_max_per_source=1,
    )
    articles = [
        build_article(1, source="Reuters", score=10),
        build_article(2, source="Reuters", score=9),
        build_article(3, source="BBC", score=8),
    ]
    llm_client = FakeLLMClient(
        [
            """
            {
              "global_briefing": [
                {
                  "url": "https://example.com/article-1",
                  "headline": "Headline 1",
                  "summary": "Summary 1.",
                  "why_it_matters": "Why 1.",
                  "source": "Reuters",
                  "date": "2026-04-01"
                },
                {
                  "url": "https://example.com/article-3",
                  "headline": "Headline 3",
                  "summary": "Summary 3.",
                  "why_it_matters": "Why 3.",
                  "source": "BBC",
                  "date": "2026-04-03"
                }
              ]
            }
            """
        ]
    )

    async def run() -> list[DigestItem]:
        return await compose_global_briefing(
            articles,
            llm_client=llm_client,
            settings=settings,
        )

    global_briefing = asyncio.run(run())
    prompt = str(llm_client.calls[0]["user_prompt"])

    assert [item.url for item in global_briefing] == [
        "https://example.com/article-1",
        "https://example.com/article-3",
    ]
    assert "https://example.com/article-1" in prompt
    assert "https://example.com/article-3" in prompt
    assert "https://example.com/article-2" not in prompt
