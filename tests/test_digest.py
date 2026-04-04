from __future__ import annotations

import asyncio

import pytest

from src.analyzer.digest import Digest, DigestCompositionError, compose_digest
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
        llm_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-4-5-haiku",
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


def build_article(index: int, score: int) -> ScoredArticle:
    return ScoredArticle(
        url=f"https://example.com/article-{index}",
        title=f"Article {index}",
        source=f"Source {index}",
        source_url="https://example.com/feed.xml",
        category="trade_media",
        published_at=f"2026-04-0{index}",
        summary=f"Summary for article {index}",
        author="Example Author",
        relevance_score=score,
        reasoning=f"Reasoning for article {index}",
    )


def test_compose_digest_parses_valid_payload() -> None:
    articles = [
        build_article(1, 10),
        build_article(2, 9),
        build_article(3, 8),
        build_article(4, 7),
    ]
    llm_client = FakeLLMClient(
        [
            """```json
            {
              "top_story": {
                "url": "https://example.com/article-1",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 1",
                "date": "2026-04-01"
              },
              "key_developments": [
                {
                  "url": "https://example.com/article-2",
                  "headline": "Key headline",
                  "summary": "Key summary.",
                  "why_it_matters": "Key implication.",
                  "source": "Source 2",
                  "date": "2026-04-02"
                }
              ],
              "on_our_radar": [
                {
                  "url": "https://example.com/article-3",
                  "headline": "Radar headline",
                  "summary": "Radar summary.",
                  "why_it_matters": "Radar implication.",
                  "source": "Source 3",
                  "date": ""
                }
              ],
              "quick_hits": [
                {
                  "url": "https://example.com/article-4",
                  "one_liner": "Quick takeaway.",
                  "source": "Source 4"
                }
              ]
            }
            ```"""
        ]
    )
    logger = DummyLogger()

    async def run() -> Digest:
        return await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            logger=logger,
        )

    digest = asyncio.run(run())

    assert digest.top_story.url == "https://example.com/article-1"
    assert digest.top_story.headline == "Top headline"
    assert digest.key_developments[0].source == "Source 2"
    assert digest.on_our_radar[0].date == ""
    assert digest.quick_hits[0].one_liner == "Quick takeaway."
    assert llm_client.calls[0]["max_tokens"] == 2600
    assert "PepsiCo" in str(llm_client.calls[0]["system_prompt"])
    assert "trusted advisor's morning brief" in str(llm_client.calls[0]["system_prompt"])
    assert "relevance_reasoning" in str(llm_client.calls[0]["user_prompt"])
    assert logger.records[-1][0] == "digest_composition_complete"


def test_compose_digest_rejects_duplicate_urls_across_sections() -> None:
    articles = [build_article(1, 10), build_article(2, 9)]
    llm_client = FakeLLMClient(
        [
            """
            {
              "top_story": {
                "url": "https://example.com/article-1",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 1",
                "date": "2026-04-01"
              },
              "key_developments": [],
              "on_our_radar": [],
              "quick_hits": [
                {
                  "url": "https://example.com/article-1",
                  "one_liner": "Repeated item.",
                  "source": "Source 1"
                }
              ]
            }
            """
        ]
    )

    async def run() -> None:
        await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
        )

    with pytest.raises(DigestCompositionError, match="reuses article URL"):
        asyncio.run(run())


def test_compose_digest_rejects_unknown_article_urls() -> None:
    articles = [build_article(1, 10)]
    llm_client = FakeLLMClient(
        [
            """
            {
              "top_story": {
                "url": "https://example.com/article-999",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 999",
                "date": "2026-04-01"
              },
              "key_developments": [],
              "on_our_radar": [],
              "quick_hits": []
            }
            """
        ]
    )

    async def run() -> None:
        await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
        )

    with pytest.raises(DigestCompositionError, match="unknown article URL"):
        asyncio.run(run())


def test_compose_digest_limits_articles_before_prompting() -> None:
    articles = [
        build_article(1, 7),
        build_article(2, 10),
        build_article(3, 9),
    ]
    llm_client = FakeLLMClient(
        [
            """
            {
              "top_story": {
                "url": "https://example.com/article-2",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 2",
                "date": "2026-04-02"
              },
              "key_developments": [
                {
                  "url": "https://example.com/article-3",
                  "headline": "Key headline",
                  "summary": "Key summary.",
                  "why_it_matters": "Key implication.",
                  "source": "Source 3",
                  "date": "2026-04-03"
                }
              ],
              "on_our_radar": [],
              "quick_hits": []
            }
            """
        ]
    )

    async def run() -> Digest:
        return await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            max_articles=2,
        )

    digest = asyncio.run(run())
    prompt = str(llm_client.calls[0]["user_prompt"])

    assert digest.top_story.url == "https://example.com/article-2"
    assert "https://example.com/article-2" in prompt
    assert "https://example.com/article-3" in prompt
    assert "https://example.com/article-1" not in prompt


def test_compose_digest_parses_embedded_fenced_json() -> None:
    articles = [build_article(1, 10), build_article(2, 9)]
    llm_client = FakeLLMClient(
        [
            """
            Here is the digest:
            ```json
            {
              "top_story": {
                "url": "https://example.com/article-1",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 1",
                "date": "2026-04-01"
              },
              "key_developments": [],
              "on_our_radar": [],
              "quick_hits": [
                {
                  "url": "https://example.com/article-2",
                  "one_liner": "Quick takeaway.",
                  "source": "Source 2"
                }
              ]
            }
            ```
            """
        ]
    )

    async def run() -> Digest:
        return await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            logger=DummyLogger(),
        )

    digest = asyncio.run(run())

    assert digest.top_story.url == "https://example.com/article-1"
    assert digest.quick_hits[0].url == "https://example.com/article-2"


def test_compose_digest_retries_after_invalid_json_response() -> None:
    articles = [build_article(1, 10), build_article(2, 9)]
    logger = DummyLogger()
    llm_client = FakeLLMClient(
        [
            "not valid json",
            """
            {
              "top_story": {
                "url": "https://example.com/article-1",
                "headline": "Top headline",
                "summary": "Top summary.",
                "why_it_matters": "Top implication.",
                "source": "Source 1",
                "date": "2026-04-01"
              },
              "key_developments": [],
              "on_our_radar": [],
              "quick_hits": [
                {
                  "url": "https://example.com/article-2",
                  "one_liner": "Quick takeaway.",
                  "source": "Source 2"
                }
              ]
            }
            """,
        ]
    )

    async def run() -> Digest:
        return await compose_digest(
            articles,
            llm_client=llm_client,
            settings=build_settings(),
            logger=logger,
        )

    digest = asyncio.run(run())

    assert digest.top_story.url == "https://example.com/article-1"
    assert len(llm_client.calls) == 2
    assert "Previous invalid response" in str(llm_client.calls[1]["user_prompt"])
    assert any(event == "digest_composition_retrying_invalid_json" for event, _payload in logger.records)
