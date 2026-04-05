from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from textwrap import dedent

import httpx
import pytest
import src.fetcher as fetcher_module
import src.fetcher.common as fetcher_common

from src.fetcher import (
    fetch_all_sources,
    fetch_all_sources_report,
    fetch_rss,
    load_source_registry,
    scrape_source,
)
from src.fetcher.common import RobotsPolicy
from src.fetcher.dedup import deduplicate_articles, normalize_url
from src.fetcher.models import RawArticle, Source
from src.fetcher.rss import RSSFetchError
from src.fetcher.scraper import JavaScriptRenderedPageError, ScrapeFetchError
from src.storage.db import get_recent_urls, save_articles
from src.utils.config import ConfigError, Settings


class DummyLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))


def test_load_source_registry_filters_inactive_and_sorts_by_tier(tmp_path) -> None:
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        dedent(
            """
            sources:
              - name: Tier 2 Source
                url: https://example.com/two.xml
                tier: 2
                method: rss
                active: true
                category: vendor
              - name: Tier 1 Source
                url: https://example.com/one.xml
                tier: 1
                method: scrape
                active: true
                category: trade_media
                selectors:
                  article: article
                  title: h2
                  link: a[href]
                  date: time
              - name: Disabled Source
                url: https://example.com/off.xml
                tier: 1
                method: rss
                active: false
                category: vendor
            """
        ),
        encoding="utf-8",
    )

    sources = load_source_registry(registry)

    assert [source.name for source in sources] == ["Tier 1 Source", "Tier 2 Source"]
    assert sources[0].selectors["article"] == "article"


def test_load_source_registry_rejects_invalid_category(tmp_path) -> None:
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        dedent(
            """
            sources:
              - name: Bad Category Source
                url: https://example.com/feed.xml
                tier: 1
                method: rss
                active: true
                category: unknown
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="category must be one of"):
        load_source_registry(registry)


def test_load_source_registry_requires_scrape_selectors(tmp_path) -> None:
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        dedent(
            """
            sources:
              - name: Missing Selectors
                url: https://example.com/blog
                tier: 1
                method: scrape
                active: true
                category: vendor
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="selectors is required for scrape sources"):
        load_source_registry(registry)


def test_normalize_url_strips_tracking_and_www() -> None:
    normalized = normalize_url(
        "https://www.example.com/story/?utm_source=rss&keep=yes&fbclid=123"
    )

    assert normalized == "https://example.com/story?keep=yes"


def test_normalize_url_removes_fragment_default_port_and_sorts_query() -> None:
    normalized = normalize_url(
        "HTTPS://www.Example.com:443/story/?b=2&utm_campaign=digest&a=1#section"
    )

    assert normalized == "https://example.com/story?a=1&b=2"


def test_deduplicate_articles_filters_recent_and_batch_duplicates() -> None:
    article_one = RawArticle(
        url="https://www.example.com/story/?utm_source=rss",
        title="Story",
        source="Example",
        source_url="https://example.com/feed.xml",
        category="trade_media",
    )
    article_two = RawArticle(
        url="https://example.com/story",
        title="Story Duplicate",
        source="Example",
        source_url="https://example.com/feed.xml",
        category="trade_media",
    )
    article_three = RawArticle(
        url="https://example.com/already-seen",
        title="Already Seen",
        source="Example",
        source_url="https://example.com/feed.xml",
        category="trade_media",
    )

    deduplicated = deduplicate_articles(
        [article_one, article_two, article_three],
        recent_urls={"https://example.com/already-seen/"},
    )

    assert [article.url for article in deduplicated] == ["https://example.com/story"]


def test_deduplicate_articles_uses_recent_urls_from_database(tmp_path) -> None:
    database_path = str(tmp_path / "dpns.db")
    current_time = datetime.now(timezone.utc)
    save_articles(
        database_path,
        [
            {
                "url": "https://www.example.com/recent-story/?utm_source=rss",
                "title": "Recent Story",
                "source": "Example",
                "fetched_at": (current_time - timedelta(days=1)).isoformat(),
            },
            {
                "url": "https://www.example.com/stale-story/",
                "title": "Stale Story",
                "source": "Example",
                "fetched_at": (current_time - timedelta(days=8)).isoformat(),
            },
        ],
    )

    deduplicated = deduplicate_articles(
        [
            RawArticle(
                url="https://example.com/recent-story",
                title="Recent Duplicate",
                source="Example",
                source_url="https://example.com/feed.xml",
                category="trade_media",
            ),
            RawArticle(
                url="https://example.com/stale-story",
                title="Stale Allowed",
                source="Example",
                source_url="https://example.com/feed.xml",
                category="trade_media",
            ),
        ],
        database_path=database_path,
    )

    assert [article.url for article in deduplicated] == ["https://example.com/stale-story"]


def test_deduplicate_articles_can_ignore_recent_urls_from_database(tmp_path) -> None:
    database_path = str(tmp_path / "dpns.db")
    current_time = datetime.now(timezone.utc)
    save_articles(
        database_path,
        [
            {
                "url": "https://www.example.com/recent-story/?utm_source=rss",
                "title": "Recent Story",
                "source": "Example",
                "fetched_at": (current_time - timedelta(days=1)).isoformat(),
            },
        ],
    )

    deduplicated = deduplicate_articles(
        [
            RawArticle(
                url="https://example.com/recent-story",
                title="Recent Duplicate",
                source="Example",
                source_url="https://example.com/feed.xml",
                category="trade_media",
            ),
        ],
        database_path=database_path,
        use_database_seen_urls=False,
    )

    assert [article.url for article in deduplicated] == ["https://example.com/recent-story"]


def test_fetch_rss_filters_old_items_and_cleans_summary() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    rss_body = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Example Feed</title>
            <item>
              <title>Fresh story</title>
              <link>https://example.com/fresh-story?utm_source=rss</link>
              <pubDate>Mon, 30 Mar 2026 08:00:00 GMT</pubDate>
              <description><![CDATA[<p>Fresh <strong>summary</strong>.</p>]]></description>
              <author>Editor</author>
            </item>
            <item>
              <title>Stale story</title>
              <link>https://example.com/stale-story</link>
              <pubDate>Thu, 26 Mar 2026 08:00:00 GMT</pubDate>
              <description><![CDATA[<p>Old summary.</p>]]></description>
            </item>
          </channel>
        </rss>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/feed.xml"
        return httpx.Response(200, text=rss_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Example RSS",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    try:
        articles = asyncio.run(fetch_rss(source, client=client, now=now))
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].title == "Fresh story"
    assert articles[0].summary == "Fresh summary."
    assert articles[0].author == "Editor"


def test_fetch_rss_parses_atom_content_and_relative_links() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    atom_body = dedent(
        """
        <?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Example Atom Feed</title>
          <entry>
            <title>Fresh atom story</title>
            <link href="/insights/fresh-atom-story" />
            <updated>2026-03-30T08:00:00Z</updated>
            <content type="html"><![CDATA[<p>Atom <strong>summary</strong>.</p>]]></content>
            <author><name>Feed Author</name></author>
          </entry>
        </feed>
        """
    ).strip()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=atom_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Example Atom",
        url="https://example.com/feed.atom",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    try:
        articles = asyncio.run(fetch_rss(source, client=client, now=now))
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].url == "https://example.com/insights/fresh-atom-story"
    assert articles[0].summary == "Atom summary."
    assert articles[0].author == "Feed Author"


def test_robots_policy_falls_back_when_httpx_is_forbidden(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"Unexpected request: {request.url}")

    def fake_fetch_robots_txt_with_urllib(*, robots_url: str, user_agent: str) -> tuple[int, str]:
        assert robots_url == "https://example.com/robots.txt"
        assert user_agent == "Mozilla/5.0"
        return 200, "User-agent: *\nDisallow: /private/\n"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        fetcher_common,
        "_fetch_robots_txt_with_urllib",
        fake_fetch_robots_txt_with_urllib,
    )
    policy = RobotsPolicy()

    try:
        assert asyncio.run(
            policy.allows(
                client=client,
                url="https://example.com/feed.xml",
                user_agent="Mozilla/5.0",
                allow_network_fallback=True,
            )
        )
        assert not asyncio.run(
            policy.allows(
                client=client,
                url="https://example.com/private/page",
                user_agent="Mozilla/5.0",
                allow_network_fallback=True,
            )
        )
    finally:
        asyncio.run(client.aclose())


def test_robots_policy_keeps_conservative_block_when_fallback_is_forbidden(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"Unexpected request: {request.url}")

    def fake_fetch_robots_txt_with_urllib(*, robots_url: str, user_agent: str) -> tuple[int, str]:
        assert robots_url == "https://example.com/robots.txt"
        assert user_agent == "Mozilla/5.0"
        return 403, "forbidden"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        fetcher_common,
        "_fetch_robots_txt_with_urllib",
        fake_fetch_robots_txt_with_urllib,
    )
    policy = RobotsPolicy()

    try:
        assert not asyncio.run(
            policy.allows(
                client=client,
                url="https://example.com/feed.xml",
                user_agent="Mozilla/5.0",
                allow_network_fallback=True,
            )
        )
    finally:
        asyncio.run(client.aclose())


def test_robots_policy_keeps_conservative_block_when_fallback_errors(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"Unexpected request: {request.url}")

    def fake_fetch_robots_txt_with_urllib(*, robots_url: str, user_agent: str) -> tuple[int, str]:
        assert robots_url == "https://example.com/robots.txt"
        assert user_agent == "Mozilla/5.0"
        return 503, "upstream error"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        fetcher_common,
        "_fetch_robots_txt_with_urllib",
        fake_fetch_robots_txt_with_urllib,
    )
    policy = RobotsPolicy()

    try:
        assert not asyncio.run(
            policy.allows(
                client=client,
                url="https://example.com/feed.xml",
                user_agent="Mozilla/5.0",
                allow_network_fallback=True,
            )
        )
    finally:
        asyncio.run(client.aclose())


def test_fetch_rss_does_not_bypass_supplied_client_for_robots_fallback(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"Unexpected request: {request.url}")

    def fail_fetch_robots_txt_with_urllib(*, robots_url: str, user_agent: str) -> tuple[int, str]:
        raise AssertionError(
            f"Unexpected stdlib robots fallback for {robots_url} with {user_agent}"
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        fetcher_common,
        "_fetch_robots_txt_with_urllib",
        fail_fetch_robots_txt_with_urllib,
    )
    source = Source(
        name="Blocked Feed",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    try:
        with pytest.raises(PermissionError, match="robots.txt disallows fetching"):
            asyncio.run(
                fetch_rss(
                    source,
                    client=client,
                    robots_policy=RobotsPolicy(),
                )
            )
    finally:
        asyncio.run(client.aclose())


def test_fetch_rss_wraps_http_status_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Broken Feed",
        url="https://example.com/missing.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    try:
        with pytest.raises(RSSFetchError, match="HTTP 404"):
            asyncio.run(fetch_rss(source, client=client))
    finally:
        asyncio.run(client.aclose())


def test_fetch_rss_raises_on_malformed_feed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not a valid feed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Malformed Feed",
        url="https://example.com/bad.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    try:
        with pytest.raises(RSSFetchError, match="Malformed feed"):
            asyncio.run(fetch_rss(source, client=client))
    finally:
        asyncio.run(client.aclose())


def test_scrape_source_extracts_recent_articles() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    html = dedent(
        """
        <html>
          <body>
            <main>
              <article>
                <h2><a href="/insights/fresh-story">Fresh Story</a></h2>
                <time datetime="2026-03-30T06:30:00+00:00"></time>
                <p>Fresh summary.</p>
                <span class="byline">Reporter</span>
              </article>
              <article>
                <h2><a href="/insights/old-story">Old Story</a></h2>
                <time datetime="2026-03-20T06:30:00+00:00"></time>
                <p>Old summary.</p>
              </article>
            </main>
          </body>
        </html>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"]
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Example Scrape",
        url="https://example.com/insights",
        tier=1,
        method="scrape",
        active=True,
        category="consulting",
        selectors={
            "article": "article",
            "title": "h2",
            "link": "a[href]",
            "date": "time",
            "summary": "p",
            "author": ".byline",
        },
    )

    try:
        articles = asyncio.run(scrape_source(source, client=client, now=now))
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].url == "https://example.com/insights/fresh-story"
    assert articles[0].summary == "Fresh summary."
    assert articles[0].author == "Reporter"


def test_scrape_source_recovers_missing_listing_dates_from_detail_pages() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    listing_html = dedent(
        """
        <html>
          <body>
            <main>
              <article>
                <h2><a href="/insights/fresh-story">Fresh Story</a></h2>
                <p>Fresh summary.</p>
              </article>
              <article>
                <h2><a href="/insights/stale-story">Stale Story</a></h2>
                <p>Stale summary.</p>
              </article>
            </main>
          </body>
        </html>
        """
    ).strip()
    fresh_detail_html = dedent(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"datePublished": "2026-03-30T06:30:00+00:00"}
            </script>
          </head>
        </html>
        """
    ).strip()
    stale_detail_html = dedent(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"datePublished": "2026-03-01T06:30:00+00:00"}
            </script>
          </head>
        </html>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/insights":
            return httpx.Response(200, text=listing_html)
        if request.url.path == "/insights/fresh-story":
            return httpx.Response(200, text=fresh_detail_html)
        if request.url.path == "/insights/stale-story":
            return httpx.Response(200, text=stale_detail_html)
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Example Scrape",
        url="https://example.com/insights",
        tier=1,
        method="scrape",
        active=True,
        category="consulting",
        selectors={
            "article": "article",
            "title": "h2",
            "link": "a[href]",
            "summary": "p",
        },
    )

    try:
        articles = asyncio.run(scrape_source(source, client=client, now=now))
    finally:
        asyncio.run(client.aclose())

    assert [article.url for article in articles] == ["https://example.com/insights/fresh-story"]
    assert articles[0].published_at == "2026-03-30T06:30:00+00:00"


def test_scrape_source_recovers_missing_listing_dates_with_managed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    listing_html = dedent(
        """
        <html>
          <body>
            <main>
              <article>
                <h2><a href="/insights/fresh-story">Fresh Story</a></h2>
                <p>Fresh summary.</p>
              </article>
              <article>
                <h2><a href="/insights/stale-story">Stale Story</a></h2>
                <p>Stale summary.</p>
              </article>
            </main>
          </body>
        </html>
        """
    ).strip()
    fresh_detail_html = dedent(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"datePublished": "2026-03-30T06:30:00+00:00"}
            </script>
          </head>
        </html>
        """
    ).strip()
    stale_detail_html = dedent(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"datePublished": "2026-03-01T06:30:00+00:00"}
            </script>
          </head>
        </html>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/insights":
            return httpx.Response(200, text=listing_html)
        if request.url.path == "/insights/fresh-story":
            return httpx.Response(200, text=fresh_detail_html)
        if request.url.path == "/insights/stale-story":
            return httpx.Response(200, text=stale_detail_html)
        raise AssertionError(f"Unexpected request: {request.url}")

    real_async_client = httpx.AsyncClient

    def build_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("src.fetcher.common.httpx.AsyncClient", build_client)

    source = Source(
        name="Example Scrape",
        url="https://example.com/insights",
        tier=1,
        method="scrape",
        active=True,
        category="consulting",
        selectors={
            "article": "article",
            "title": "h2",
            "link": "a[href]",
            "summary": "p",
        },
    )

    articles = asyncio.run(scrape_source(source, now=now))

    assert [article.url for article in articles] == ["https://example.com/insights/fresh-story"]
    assert articles[0].published_at == "2026-03-30T06:30:00+00:00"


def test_scrape_source_drops_entries_when_no_listing_or_detail_date_can_be_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    listing_html = dedent(
        """
        <html>
          <body>
            <main>
              <article>
                <h2><a href="/insights/undated-story">Undated Story</a></h2>
                <p>Undated summary.</p>
              </article>
            </main>
          </body>
        </html>
        """
    ).strip()
    undated_detail_html = "<html><body><h1>Undated Story</h1></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/insights":
            return httpx.Response(200, text=listing_html)
        if request.url.path == "/insights/undated-story":
            return httpx.Response(200, text=undated_detail_html)
        raise AssertionError(f"Unexpected request: {request.url}")

    real_async_client = httpx.AsyncClient

    def build_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("src.fetcher.common.httpx.AsyncClient", build_client)

    source = Source(
        name="Example Scrape",
        url="https://example.com/insights",
        tier=1,
        method="scrape",
        active=True,
        category="consulting",
        selectors={
            "article": "article",
            "title": "h2",
            "link": "a[href]",
            "summary": "p",
        },
    )

    articles = asyncio.run(scrape_source(source, now=now))

    assert articles == []


def test_scrape_source_falls_back_to_anchor_scan_and_meta_author() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    html = dedent(
        """
        <html>
          <head>
            <meta name="author" content="Site Editor" />
          </head>
          <body>
            <section>
              <div class="story">
                <a href="/news/fresh-story">Fresh Story With Enough Words</a>
                <time datetime="2026-03-30T06:30:00+00:00"></time>
                <p>Fallback summary.</p>
              </div>
            </section>
          </body>
        </html>
        """
    ).strip()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Fallback Scrape",
        url="https://example.com/resources",
        tier=1,
        method="scrape",
        active=True,
        category="consulting",
        selectors={
            "article": ".missing-container",
            "title": ".missing-title",
            "link": ".missing-link",
        },
    )

    try:
        articles = asyncio.run(scrape_source(source, client=client, now=now))
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].url == "https://example.com/news/fresh-story"
    assert articles[0].summary == "Fallback summary."
    assert articles[0].author == "Site Editor"


def test_scrape_source_wraps_http_status_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="Broken Scrape",
        url="https://example.com/missing",
        tier=1,
        method="scrape",
        active=True,
        category="trade_media",
    )

    try:
        with pytest.raises(ScrapeFetchError, match="HTTP 404"):
            asyncio.run(scrape_source(source, client=client))
    finally:
        asyncio.run(client.aclose())


def test_scrape_source_flags_javascript_rendered_pages() -> None:
    html = dedent(
        """
        <html>
          <body>
            <div id="__next"></div>
            <noscript>Please enable JavaScript to run this app.</noscript>
            <script>window.__NEXT_DATA__ = {};</script>
          </body>
        </html>
        """
    ).strip()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = Source(
        name="JS App Source",
        url="https://example.com/app",
        tier=1,
        method="scrape",
        active=True,
        category="vendor",
    )

    try:
        with pytest.raises(JavaScriptRenderedPageError, match="Playwright"):
            asyncio.run(scrape_source(source, client=client))
    finally:
        asyncio.run(client.aclose())


def test_fetch_all_sources_deduplicates_and_saves_articles(tmp_path) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    database_path = str(tmp_path / "dpns.db")
    logger = DummyLogger()
    settings = _settings(database_path)
    rss_one = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Shared story</title>
              <link>https://www.example.com/shared-story/?utm_source=rss</link>
              <pubDate>Mon, 30 Mar 2026 08:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
    ).strip()
    rss_two = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Shared story duplicate</title>
              <link>https://example.com/shared-story</link>
              <pubDate>Mon, 30 Mar 2026 07:30:00 GMT</pubDate>
            </item>
            <item>
              <title>Unique story</title>
              <link>https://example.com/unique-story</link>
              <pubDate>Mon, 30 Mar 2026 06:30:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/feed-one.xml":
            return httpx.Response(200, text=rss_one)
        if request.url.path == "/feed-two.xml":
            return httpx.Response(200, text=rss_two)
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sources = [
        Source(
            name="Feed One",
            url="https://example.com/feed-one.xml",
            tier=1,
            method="rss",
            active=True,
            category="trade_media",
        ),
        Source(
            name="Feed Two",
            url="https://example.com/feed-two.xml",
            tier=1,
            method="rss",
            active=True,
            category="trade_media",
        ),
    ]

    try:
        articles = asyncio.run(
            fetch_all_sources(
                sources=sources,
                settings=settings,
                database_path=database_path,
                logger=logger,
                client=client,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [article.url for article in articles] == [
        "https://example.com/shared-story",
        "https://example.com/unique-story",
    ]
    assert get_recent_urls(database_path, days=7) == {
        "https://example.com/shared-story",
        "https://example.com/unique-story",
    }
    assert any(event == "fetch_all_sources_complete" for event, _ in logger.events)


def test_fetch_all_sources_loads_registry_when_sources_not_provided(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    database_path = str(tmp_path / "dpns.db")
    logger = DummyLogger()
    settings = _settings(database_path)
    rss_body = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Registry story</title>
              <link>https://example.com/registry-story</link>
              <pubDate>Mon, 30 Mar 2026 08:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
    ).strip()
    registry_source = Source(
        name="Registry Feed",
        url="https://example.com/registry.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    def fake_load_source_registry() -> list[Source]:
        return [registry_source]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/registry.xml":
            return httpx.Response(200, text=rss_body)
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(fetcher_module, "load_source_registry", fake_load_source_registry)

    try:
        articles = asyncio.run(
            fetch_all_sources(
                settings=settings,
                database_path=database_path,
                logger=logger,
                client=client,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [article.url for article in articles] == ["https://example.com/registry-story"]
    assert get_recent_urls(database_path, days=7) == {"https://example.com/registry-story"}


def test_fetch_all_sources_continues_when_one_source_fails(tmp_path) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    database_path = str(tmp_path / "dpns.db")
    logger = DummyLogger()
    settings = _settings(database_path)
    rss_body = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Healthy story</title>
              <link>https://example.com/healthy-story</link>
              <pubDate>Mon, 30 Mar 2026 08:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/healthy.xml":
            return httpx.Response(200, text=rss_body)
        if request.url.path == "/broken.xml":
            return httpx.Response(404, text="missing")
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sources = [
        Source(
            name="Healthy Feed",
            url="https://example.com/healthy.xml",
            tier=1,
            method="rss",
            active=True,
            category="trade_media",
        ),
        Source(
            name="Broken Feed",
            url="https://example.com/broken.xml",
            tier=1,
            method="rss",
            active=True,
            category="trade_media",
        ),
    ]

    try:
        articles = asyncio.run(
            fetch_all_sources(
                sources=sources,
                settings=settings,
                database_path=database_path,
                logger=logger,
                client=client,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [article.url for article in articles] == ["https://example.com/healthy-story"]
    failure_event = next(
        kwargs for event, kwargs in logger.events if event == "source_fetch_failed"
    )
    complete_event = next(
        kwargs for event, kwargs in logger.events if event == "fetch_all_sources_complete"
    )

    assert failure_event["source"] == "Broken Feed"
    assert complete_event["sources_attempted"] == 2
    assert complete_event["sources_succeeded"] == 1
    assert complete_event["sources_failed"] == 1
    assert complete_event["articles_found"] == 1


def test_fetch_all_sources_report_can_skip_database_persistence(tmp_path) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    database_path = str(tmp_path / "dpns.db")
    logger = DummyLogger()
    settings = _settings(database_path)
    rss_body = dedent(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Preview story</title>
              <link>https://example.com/preview-story</link>
              <pubDate>Mon, 30 Mar 2026 08:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
    ).strip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/preview.xml":
            return httpx.Response(200, text=rss_body)
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sources = [
        Source(
            name="Preview Feed",
            url="https://example.com/preview.xml",
            tier=1,
            method="rss",
            active=True,
            category="trade_media",
        )
    ]

    try:
        summary = asyncio.run(
            fetch_all_sources_report(
                sources=sources,
                settings=settings,
                database_path=database_path,
                logger=logger,
                client=client,
                now=now,
                persist_to_db=False,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [article.url for article in summary.articles] == ["https://example.com/preview-story"]
    assert summary.articles_saved == 0
    assert get_recent_urls(database_path, days=7) == set()


def test_fetch_all_sources_report_enables_robots_fallback_for_managed_client(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc)
    database_path = str(tmp_path / "dpns.db")
    logger = DummyLogger()
    settings = _settings(database_path)
    source = Source(
        name="Managed Client Feed",
        url="https://example.com/managed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )

    async def fake_fetch_rss(source_arg: Source, **kwargs) -> list[RawArticle]:
        assert source_arg == source
        assert kwargs["allow_robots_network_fallback"] is True
        assert isinstance(kwargs["client"], httpx.AsyncClient)
        return [
            RawArticle(
                url="https://example.com/managed-story",
                title="Managed story",
                source=source_arg.name,
                source_url=source_arg.url,
                category=source_arg.category,
                published_at="2026-03-30T08:00:00+00:00",
                fetched_at=now.isoformat(),
            )
        ]

    monkeypatch.setattr(fetcher_module, "fetch_rss", fake_fetch_rss)

    summary = asyncio.run(
        fetch_all_sources_report(
            sources=[source],
            settings=settings,
            database_path=database_path,
            logger=logger,
            now=now,
            persist_to_db=False,
        )
    )

    assert [article.url for article in summary.articles] == ["https://example.com/managed-story"]
    assert summary.sources_succeeded == 1
    assert summary.sources_failed == 0


def _settings(database_path: str) -> Settings:
    return Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Europe/Madrid",
        llm_scoring_model="scoring-model",
        llm_digest_model="digest-model",
        llm_model_fallback="fallback",
        database_path=database_path,
        log_level="INFO",
        log_file="data/logs/dpns.jsonl",
        dry_run=True,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=0.0,
    )
