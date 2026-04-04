from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

from src.fetcher.models import Source


@pytest.fixture
def seed_sources_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "seed_sources.py"
    spec = importlib.util.spec_from_file_location("seed_sources_test_module", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load scripts/seed_sources.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validate_all_can_include_inactive(monkeypatch: pytest.MonkeyPatch, seed_sources_module) -> None:
    captured: list[bool] = []

    def fake_load_source_registry(*, active_only: bool = True):
        captured.append(active_only)
        return []

    monkeypatch.setattr(seed_sources_module, "load_source_registry", fake_load_source_registry)

    asyncio.run(
        seed_sources_module.validate_all(timeout=1.0, verbose=False, include_inactive=False)
    )
    asyncio.run(
        seed_sources_module.validate_all(timeout=1.0, verbose=False, include_inactive=True)
    )

    assert captured == [True, False]


def test_summarize_results_counts_warnings_and_failures(seed_sources_module) -> None:
    source = Source(
        name="Example",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )
    ok_result = seed_sources_module.SourceCheckResult(source=source, reachable=True)
    warn_result = seed_sources_module.SourceCheckResult(
        source=source,
        reachable=True,
        warnings=["selector warning"],
    )
    fail_result = seed_sources_module.SourceCheckResult(
        source=source,
        reachable=False,
        error="HTTP 500",
    )

    counts = seed_sources_module.summarize_results([ok_result, warn_result, fail_result])

    assert counts.ok == 1
    assert counts.warnings == 1
    assert counts.failed == 1


def test_summarize_results_treats_validation_errors_as_failures(seed_sources_module) -> None:
    source = Source(
        name="Example",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )
    validation_error = seed_sources_module.SourceCheckResult(
        source=source,
        reachable=True,
        error="Malformed feed for Example",
    )

    counts = seed_sources_module.summarize_results([validation_error])

    assert counts.ok == 0
    assert counts.warnings == 0
    assert counts.failed == 1


def test_check_source_rss_reports_feed_entries(seed_sources_module) -> None:
    source = Source(
        name="Example RSS",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )
    rss_body = """
    <rss version="2.0">
      <channel>
        <title>Example Feed</title>
        <item>
          <title>Article One</title>
          <link>https://example.com/article-1</link>
        </item>
      </channel>
    </rss>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=rss_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            seed_sources_module.check_source(
                source,
                client,
                timeout=1.0,
                verbose=False,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.reachable is True
    assert result.feed_format
    assert result.rss_entries_found == 1
    assert result.articles_found == 1
    assert result.warnings == []
    assert result.error is None


def test_check_source_rss_marks_malformed_empty_feeds_as_failures(seed_sources_module) -> None:
    source = Source(
        name="Broken RSS",
        url="https://example.com/broken.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )
    malformed_rss = "<rss><channel><title>Broken</title></rss>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=malformed_rss)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            seed_sources_module.check_source(
                source,
                client,
                timeout=1.0,
                verbose=False,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.reachable is True
    assert result.rss_entries_found == 0
    assert result.error is not None
    assert "Malformed feed for Broken RSS" in result.error
    assert result.warnings == []


def test_check_source_scrape_reports_extracted_articles(seed_sources_module) -> None:
    source = Source(
        name="Example Scrape",
        url="https://example.com/news",
        tier=1,
        method="scrape",
        active=True,
        category="trade_media",
        selectors={
            "article": "article",
            "title": "h2",
            "link": "a",
            "date": "time",
        },
    )
    html = """
    <html>
      <body>
        <article>
          <h2>Modern procurement rollout</h2>
          <a href="/article-1">Read more</a>
          <time datetime="2026-04-01T08:00:00+00:00">Apr 1, 2026</time>
        </article>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(
            seed_sources_module.check_source(
                source,
                client,
                timeout=1.0,
                verbose=False,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert result.reachable is True
    assert result.containers_found == 1
    assert result.titles_found == 1
    assert result.links_found == 1
    assert result.articles_found == 1
    assert result.warnings == []
