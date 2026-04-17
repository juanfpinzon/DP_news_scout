from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from textwrap import dedent

import httpx

from src.fetcher.common import DomainRateLimiter, RobotsPolicy
from src.fetcher.models import SearchFallbackConfig, Source
from src.fetcher.search_fallback import (
    SearchFallbackAllowlist,
    SearchFallbackPublisher,
    load_effective_search_allowlist,
    resolve_allowed_publisher,
    search_fallback_articles,
)
from src.utils.config import Settings


class _CapturingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs) -> None:
        self.records.append((event, kwargs))


class _RecordingRateLimiter:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def wait(self, url: str) -> None:
        self.urls.append(url)


class _RecordingRobotsPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def allows(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        user_agent: str,
        allow_network_fallback: bool,
    ) -> bool:
        self.calls.append((url, user_agent))
        return True


def test_load_effective_search_allowlist_auto_includes_trusted_source_categories(
    tmp_path,
) -> None:
    allowlist_path = tmp_path / "allowlist.yaml"
    allowlist_path.write_text(
        dedent(
            """
            auto_include_source_categories:
              - trade_media
            publishers:
              - domain: reuters.com
                label: Reuters
                group: mainstream
                active: true
            deny_domains:
              - wikipedia.org
            """
        ),
        encoding="utf-8",
    )

    allowlist = load_effective_search_allowlist(
        path=allowlist_path,
        source_registry=[
            Source(
                name="Trusted Trade",
                url="https://trade.example.com/feed.xml",
                tier=1,
                method="rss",
                active=True,
                category="trade_media",
            ),
            Source(
                name="Vendor Blog",
                url="https://vendor.example.com/blog",
                tier=1,
                method="scrape",
                active=True,
                category="vendor",
                selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
            ),
        ],
    )

    assert "reuters.com" in allowlist.publishers
    assert "trade.example.com" in allowlist.publishers
    assert "vendor.example.com" not in allowlist.publishers


def test_resolve_allowed_publisher_matches_subdomains_and_respects_denylist() -> None:
    allowlist = SearchFallbackAllowlist(
        publishers={
            "ft.com": SearchFallbackPublisher(
                domain="ft.com",
                label="Financial Times",
                group="mainstream",
                active=True,
            )
        },
        deny_domains={"linkedin.com"},
        auto_include_source_categories=(),
    )

    publisher = resolve_allowed_publisher(
        "https://www.ft.com/content/example-story",
        allowlist=allowlist,
    )

    assert publisher is not None
    assert publisher.label == "Financial Times"
    assert (
        resolve_allowed_publisher(
            "https://www.linkedin.com/posts/example",
            allowlist=allowlist,
        )
        is None
    )


def test_search_fallback_articles_returns_allowlisted_article(monkeypatch) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta name="description" content="Recovered summary">
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                        <meta name="author" content="Reuters Staff">
                      </head>
                      <body><article>Story body</article></body>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=2,
        ),
    )
    settings = _settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=settings,
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].source == "Reuters"
    assert articles[0].origin_source == "SAP Ariba"
    assert articles[0].discovery_method == "search_fallback"
    assert articles[0].summary == "Recovered summary"
    assert articles[0].category == "mainstream"


def test_search_fallback_articles_rejects_redirects_to_non_allowlisted_domains(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    messages: list[str] = []
    logger = _CapturingLogger()
    requested_urls: list[str] = []
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example/redirected-story"},
            )
        if request.url.host == "evil.example":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Malicious redirect</title>
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
                logger=logger,
                progress_callback=messages.append,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert articles == []
    assert "https://evil.example/redirected-story" not in requested_urls
    assert messages == [
        "Search fallback for SAP Ariba: Brave returned 1 result; 1 blocked by allowlist."
    ]
    assert logger.records[-1][1]["rejected_domain_not_allowed"] == 1


def test_search_fallback_articles_preserves_final_allowlisted_redirect_url(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/short-link/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/short-link/":
            return httpx.Response(
                302,
                headers={"Location": "/world/europe/example-story/"},
            )
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta name="description" content="Recovered summary">
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].url == "https://www.reuters.com/world/europe/example-story/"
    assert articles[0].source == "Reuters"


def test_search_fallback_articles_keeps_same_user_agent_across_redirect_chain(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    request_user_agents: list[tuple[str, str]] = []
    robots_policy = _RecordingRobotsPolicy()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )
    monkeypatch.setattr(
        "src.fetcher.search_fallback.build_request_headers",
        lambda _source_name, url: {
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "UA-short" if "short-link" in url else "UA-final",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        request_user_agents.append((str(request.url), request.headers["User-Agent"]))
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/short-link/",
                            "title": "Recovered headline",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/short-link/":
            return httpx.Response(
                302,
                headers={"Location": "/world/europe/example-story/"},
            )
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=robots_policy,
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert request_user_agents == [
        ("https://api.search.brave.com/res/v1/news/search?q=%22SAP+Ariba%22+procurement&count=6", "python-httpx/0.28.1"),
        ("https://www.reuters.com/world/europe/short-link/", "UA-short"),
        ("https://www.reuters.com/world/europe/example-story/", "UA-short"),
    ]
    assert robots_policy.calls == [
        ("https://www.reuters.com/world/europe/short-link/", "UA-short"),
        ("https://www.reuters.com/world/europe/example-story/", "UA-short"),
    ]


def test_search_fallback_articles_rate_limits_redirected_candidate_once(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    rate_limiter = _RecordingRateLimiter()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/short-link/",
                            "title": "Recovered headline",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/short-link/":
            return httpx.Response(
                302,
                headers={"Location": "/world/europe/example-story/"},
            )
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=rate_limiter,
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert rate_limiter.urls == [
        "https://api.search.brave.com/res/v1/news/search",
        "https://www.reuters.com/world/europe/short-link/",
    ]


def test_search_fallback_articles_rejects_redirect_chains_longer_than_two_hops(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    messages: list[str] = []
    logger = _CapturingLogger()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/hop-1/",
                            "title": "Recovered headline",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/hop-1/":
            return httpx.Response(302, headers={"Location": "/world/europe/hop-2/"})
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/hop-2/":
            return httpx.Response(302, headers={"Location": "/world/europe/hop-3/"})
        if request.url.host == "www.reuters.com" and request.url.path == "/world/europe/hop-3/":
            return httpx.Response(302, headers={"Location": "/world/europe/final-story/"})
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
                logger=logger,
                progress_callback=messages.append,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert articles == []
    assert messages == [
        "Search fallback for SAP Ariba: Brave returned 1 result; 1 article fetch failed."
    ]
    assert logger.records[-1][1]["rejected_candidate_fetch_failed"] == 1


def test_search_fallback_articles_allow_global_news_publishers_for_procurement_sources(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="global_news",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=1,
        ),
    )
    settings = _settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=settings,
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(articles) == 1
    assert articles[0].origin_source == "SAP Ariba"
    assert articles[0].category == "global_news"


def test_search_fallback_articles_skips_candidates_blocked_by_candidate_robots(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=2,
        ),
    )
    settings = _settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=settings,
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert articles == []


def test_search_fallback_articles_retries_transient_brave_errors(monkeypatch) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Recovered headline",
                            "description": "Recovered summary",
                        }
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Recovered headline</title>
                        <meta name="description" content="Recovered summary">
                        <meta property="article:published_time" content="2026-04-04T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=2,
        ),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert attempts["count"] == 2
    assert len(articles) == 1


def test_search_fallback_articles_emits_zero_result_summary(monkeypatch) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    messages: list[str] = []
    logger = _CapturingLogger()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={},
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=2,
        ),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
                logger=logger,
                progress_callback=messages.append,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert articles == []
    assert messages == ["Search fallback for SAP Ariba: Brave returned 0 results."]
    assert logger.records[-1][0] == "source_search_fallback_complete"
    assert logger.records[-1][1]["summary"] == "Brave returned 0 results."


def test_search_fallback_articles_summarizes_allowlist_and_stale_rejections(monkeypatch) -> None:
    now = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    messages: list[str] = []
    logger = _CapturingLogger()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr(
        "src.fetcher.search_fallback.load_effective_search_allowlist",
        lambda: SearchFallbackAllowlist(
            publishers={
                "reuters.com": SearchFallbackPublisher(
                    domain="reuters.com",
                    label="Reuters",
                    group="mainstream",
                    active=True,
                )
            },
            deny_domains=set(),
            auto_include_source_categories=(),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.com/not-allowlisted",
                            "title": "Ignore me",
                        },
                        {
                            "url": "https://www.reuters.com/world/europe/example-story/",
                            "title": "Old Reuters story",
                            "description": "Recovered summary",
                        },
                    ]
                },
            )
        if request.url.host == "www.reuters.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "www.reuters.com":
            return httpx.Response(
                200,
                text=dedent(
                    """
                    <html>
                      <head>
                        <title>Old Reuters story</title>
                        <meta property="article:published_time" content="2026-03-20T07:00:00Z">
                      </head>
                    </html>
                    """
                ).strip(),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    source = Source(
        name="SAP Ariba",
        url="https://news.sap.com/tags/sap-ariba/",
        tier=2,
        method="scrape",
        active=False,
        category="vendor",
        selectors={"article": "article", "title": "h2", "link": "a[href]", "date": "time"},
        fallback_search=SearchFallbackConfig(
            configured=True,
            enabled=True,
            include_when_inactive=True,
            query="\"SAP Ariba\" procurement",
            max_results=2,
        ),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        articles = asyncio.run(
            search_fallback_articles(
                source,
                client=client,
                settings=_settings(),
                rate_limiter=DomainRateLimiter(0),
                robots_policy=RobotsPolicy(),
                allow_robots_network_fallback=False,
                now=now,
                logger=logger,
                progress_callback=messages.append,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert articles == []
    assert messages == [
        "Search fallback for SAP Ariba: Brave returned 2 results; 1 blocked by allowlist; 1 stale."
    ]
    assert logger.records[-1][1]["rejected_domain_not_allowed"] == 1
    assert logger.records[-1][1]["rejected_stale"] == 1


def _settings() -> Settings:
    return Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Europe/Madrid",
        llm_scoring_model="scoring-model",
        llm_digest_model="digest-model",
        llm_model_fallback="fallback-model",
        database_path="data/dpns.db",
        log_level="INFO",
        log_file="data/logs/dpns.jsonl",
        dry_run=True,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=168,
        dedup_window_days=14,
        request_timeout_seconds=15.0,
        rate_limit_seconds=0.0,
        max_digest_items_per_source=3,
        email_max_width_px=880,
        issue_number_override=0,
        recency_priority_window_days=7,
        reuse_seen_db_window_days=14,
        search_fallback_enabled=True,
        search_fallback_provider="brave",
        search_fallback_timeout_seconds=15.0,
        search_fallback_max_results_per_source=3,
    )
