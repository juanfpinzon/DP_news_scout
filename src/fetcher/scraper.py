from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from src.fetcher.common import (
    DomainRateLimiter,
    RobotsPolicy,
    build_request_headers,
    clean_text,
    coerce_absolute_url,
    is_recent_enough,
    looks_like_content_url,
    managed_async_client,
    parse_datetime,
    same_domain,
)
from src.fetcher.models import RawArticle, Source

FALLBACK_ARTICLE_SELECTOR = (
    "article, .post, .card, .resource-item, .listing-item, .teaser, "
    ".summary-item, .news-item, .story-card, li"
)
FALLBACK_TITLE_SELECTOR = "h1, h2, h3, h4, .title, .card-title, .summary-item__title"
FALLBACK_LINK_SELECTOR = "a[href]"
FALLBACK_DATE_SELECTOR = (
    "time, .date, .published, .post-date, .timestamp, .news-date, .carddate, .blogdate"
)
FALLBACK_SUMMARY_SELECTOR = "p, .excerpt, .summary, .description"
FALLBACK_AUTHOR_SELECTOR = (
    ".author, .authors, .byline, [rel='author'], [itemprop='author'], .cmp-teaser__author"
)
FALLBACK_META_AUTHOR_SELECTORS = (
    "meta[name='author']",
    "meta[property='article:author']",
    "meta[name='parsely-author']",
    "meta[name='dc.creator']",
)
JAVASCRIPT_REQUIRED_TEXT_MARKERS = (
    "enable javascript",
    "please turn javascript on",
    "javascript is required",
    "requires javascript",
    "javascript disabled",
)
JAVASCRIPT_APP_ROOT_SELECTORS = (
    "#__next",
    "#__nuxt",
    "#app",
    "#root",
    "[data-reactroot]",
    "[ng-version]",
)
JAVASCRIPT_BOOTSTRAP_MARKERS = (
    "__NEXT_DATA__",
    "__NUXT__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "webpackChunk",
)
DETAIL_META_DATE_SELECTORS = (
    "meta[property='article:published_time']",
    "meta[name='article:published_time']",
    "meta[property='og:published_time']",
    "meta[name='parsely-pub-date']",
    "meta[name='pubdate']",
)
DETAIL_VISIBLE_DATE_SELECTORS = (
    "time[datetime]",
    "time",
    "[itemprop='datePublished']",
    ".blogdate",
    ".carddate",
)


class ScrapeFetchError(RuntimeError):
    """Raised when a source page cannot be scraped."""


class JavaScriptRenderedPageError(ScrapeFetchError):
    """Raised when a page likely requires browser rendering."""


async def scrape_source(
    source: Source,
    *,
    client: httpx.AsyncClient | None = None,
    lookback_hours: int = 48,
    max_articles: int = 10,
    timeout_seconds: float = 15.0,
    now: datetime | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    robots_policy: RobotsPolicy | None = None,
) -> list[RawArticle]:
    headers = build_request_headers(source.name, source.url)
    active_now = now or datetime.now(timezone.utc)
    async with managed_async_client(client, timeout_seconds=timeout_seconds) as active_client:
        if robots_policy is not None:
            allowed = await robots_policy.allows(
                client=active_client,
                url=source.url,
                user_agent=headers["User-Agent"],
            )
            if not allowed:
                raise PermissionError(f"robots.txt disallows fetching {source.url}")

        if rate_limiter is not None:
            await rate_limiter.wait(source.url)

        try:
            response = await active_client.get(source.url, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ScrapeFetchError(
                f"Timed out fetching source page for {source.name}: {source.url}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise ScrapeFetchError(
                f"Source page returned HTTP {status_code} for {source.name}: {source.url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScrapeFetchError(
                f"Failed to fetch source page for {source.name}: {source.url}"
            ) from exc

    soup = BeautifulSoup(response.text, "html.parser")
    articles = _parse_article_containers(
        soup=soup,
        source=source,
        now=active_now,
        lookback_hours=lookback_hours,
        max_articles=max_articles,
    )
    articles = await _recover_missing_dates(
        articles=articles,
        source=source,
        client=active_client,
        now=active_now,
        lookback_hours=lookback_hours,
        max_articles=max_articles,
        rate_limiter=rate_limiter,
        robots_policy=robots_policy,
    )
    if articles:
        return articles

    fallback_articles = _fallback_anchor_scan(
        soup=soup,
        source=source,
        now=active_now,
        lookback_hours=lookback_hours,
        max_articles=max_articles,
    )
    fallback_articles = await _recover_missing_dates(
        articles=fallback_articles,
        source=source,
        client=active_client,
        now=active_now,
        lookback_hours=lookback_hours,
        max_articles=max_articles,
        rate_limiter=rate_limiter,
        robots_policy=robots_policy,
    )
    if fallback_articles:
        return fallback_articles

    if _looks_like_javascript_rendered_page(soup, response.text):
        raise JavaScriptRenderedPageError(
            f"{source.name} appears to rely on client-side rendering and may require "
            f"Playwright or another browser-based fetcher: {source.url}"
        )

    return []


def _parse_article_containers(
    *,
    soup: BeautifulSoup,
    source: Source,
    now: datetime,
    lookback_hours: int,
    max_articles: int,
) -> list[RawArticle]:
    selectors = source.selectors
    container_selector = selectors.get("article", FALLBACK_ARTICLE_SELECTOR)
    title_selector = selectors.get("title", FALLBACK_TITLE_SELECTOR)
    link_selector = selectors.get("link", FALLBACK_LINK_SELECTOR)
    date_selector = selectors.get("date", FALLBACK_DATE_SELECTOR)
    summary_selector = selectors.get("summary", FALLBACK_SUMMARY_SELECTOR)
    author_selector = selectors.get("author", "")
    link_attr = selectors.get("link_attr", "href")
    date_attr = selectors.get("date_attr", "datetime")

    containers = soup.select(container_selector)
    articles: list[RawArticle] = []
    seen_urls: set[str] = set()
    fetched_at = now.isoformat()

    for container in containers:
        title = _extract_text(container, title_selector)
        url = _extract_url(container, source.url, link_selector, link_attr)
        if not title or not url or url in seen_urls or not same_domain(source.url, url):
            continue

        published_at = _extract_date(container, date_selector, date_attr)
        if not is_recent_enough(published_at, now=now, lookback_hours=lookback_hours):
            continue

        articles.append(
            RawArticle(
                url=url,
                title=title,
                source=source.name,
                source_url=source.url,
                category=source.category,
                published_at=published_at.isoformat() if published_at else None,
                fetched_at=fetched_at,
                summary=_extract_text(container, summary_selector),
                author=_extract_author(
                    container=container,
                    document=soup,
                    selector=author_selector,
                ),
            )
        )
        seen_urls.add(url)

        if len(articles) >= max_articles:
            break

    return articles


def _fallback_anchor_scan(
    *,
    soup: BeautifulSoup,
    source: Source,
    now: datetime,
    lookback_hours: int,
    max_articles: int,
) -> list[RawArticle]:
    base_domain = urlparse(source.url).netloc.lower().removeprefix("www.")
    articles: list[RawArticle] = []
    seen_urls: set[str] = set()
    fetched_at = now.isoformat()

    for anchor in soup.select("a[href]"):
        href = coerce_absolute_url(source.url, anchor.get("href"))
        title = clean_text(anchor.get_text(" ", strip=True))
        if (
            not href
            or not title
            or len(title) < 12
            or href in seen_urls
            or not looks_like_content_url(base_domain, href)
        ):
            continue

        container = anchor.parent if isinstance(anchor.parent, Tag) else anchor
        published_at = _extract_date(container, FALLBACK_DATE_SELECTOR, "datetime")
        if not is_recent_enough(published_at, now=now, lookback_hours=lookback_hours):
            continue

        articles.append(
            RawArticle(
                url=href,
                title=title,
                source=source.name,
                source_url=source.url,
                category=source.category,
                published_at=published_at.isoformat() if published_at else None,
                fetched_at=fetched_at,
                summary=_extract_text(container, FALLBACK_SUMMARY_SELECTOR),
                author=_extract_author(container=container, document=soup),
            )
        )
        seen_urls.add(href)

        if len(articles) >= max_articles:
            break

    return articles


async def _recover_missing_dates(
    *,
    articles: list[RawArticle],
    source: Source,
    client: httpx.AsyncClient,
    now: datetime,
    lookback_hours: int,
    max_articles: int,
    rate_limiter: DomainRateLimiter | None,
    robots_policy: RobotsPolicy | None,
) -> list[RawArticle]:
    for article in articles:
        if article.published_at:
            continue
        recovered = await _fetch_article_published_at(
            article_url=article.url,
            source=source,
            client=client,
            rate_limiter=rate_limiter,
            robots_policy=robots_policy,
        )
        if recovered is not None:
            article.published_at = recovered.isoformat()

    filtered = [
        article
        for article in articles
        if is_recent_enough(
            parse_datetime(article.published_at),
            now=now,
            lookback_hours=lookback_hours,
        )
    ]
    filtered.sort(key=_article_sort_key, reverse=True)
    return filtered[:max_articles]


async def _fetch_article_published_at(
    *,
    article_url: str,
    source: Source,
    client: httpx.AsyncClient,
    rate_limiter: DomainRateLimiter | None,
    robots_policy: RobotsPolicy | None,
) -> datetime | None:
    headers = build_request_headers(source.name, article_url)
    if robots_policy is not None:
        allowed = await robots_policy.allows(
            client=client,
            url=article_url,
            user_agent=headers["User-Agent"],
        )
        if not allowed:
            return None

    if rate_limiter is not None:
        await rate_limiter.wait(article_url)

    try:
        response = await client.get(article_url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError:
        return None

    return _extract_document_date(BeautifulSoup(response.text, "html.parser"))


def _extract_document_date(document: BeautifulSoup) -> datetime | None:
    for selector in DETAIL_META_DATE_SELECTORS:
        node = document.select_one(selector)
        if node is None:
            continue
        parsed = parse_datetime(node.get("content") or node.get("datetime"))
        if parsed is not None:
            return parsed

    for node in document.select("script[type='application/ld+json']"):
        parsed = _extract_json_ld_date(node.string or node.get_text(" ", strip=True))
        if parsed is not None:
            return parsed

    for selector in DETAIL_VISIBLE_DATE_SELECTORS:
        node = document.select_one(selector)
        if node is None:
            continue
        parsed = parse_datetime(
            node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
        )
        if parsed is not None:
            return parsed

    return None


def _extract_json_ld_date(raw_json: str | None) -> datetime | None:
    if not raw_json:
        return None

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return None

    return _walk_json_ld_for_date(payload)


def _walk_json_ld_for_date(value: object) -> datetime | None:
    if isinstance(value, dict):
        for key in ("datePublished", "dateCreated", "uploadDate"):
            parsed = parse_datetime(value.get(key))
            if parsed is not None:
                return parsed
        for nested in value.values():
            parsed = _walk_json_ld_for_date(nested)
            if parsed is not None:
                return parsed
        return None

    if isinstance(value, list):
        for item in value:
            parsed = _walk_json_ld_for_date(item)
            if parsed is not None:
                return parsed

    return None


def _article_sort_key(article: RawArticle) -> tuple[str, str]:
    published = article.published_at or ""
    return (published, article.url)


def _extract_text(container: Tag, selector: str) -> str | None:
    if not selector:
        return None

    for node in container.select(selector):
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return None


def _extract_url(
    container: Tag,
    base_url: str,
    selector: str,
    attribute: str,
) -> str | None:
    # Check the container itself first (handles cases where the article
    # container is an <a> tag, e.g. Webflow .w-inline-block links).
    if container.name == "a" and container.get("href"):
        url = coerce_absolute_url(base_url, container.get("href"))
        if url:
            return url

    for node in container.select(selector):
        if attribute in node.attrs:
            url = coerce_absolute_url(base_url, node.get(attribute))
            if url:
                return url
        if node.name == "a":
            url = coerce_absolute_url(base_url, node.get("href"))
            if url:
                return url
    return None


def _extract_date(container: Tag, selector: str, attribute: str) -> datetime | None:
    for node in container.select(selector):
        raw_value = node.get(attribute) or node.get("content") or node.get_text(" ", strip=True)
        parsed = parse_datetime(raw_value)
        if parsed is not None:
            return parsed

    raw_value = container.get(attribute) or container.get_text(" ", strip=True)
    return parse_datetime(raw_value)


def _extract_author(
    *,
    container: Tag,
    document: BeautifulSoup,
    selector: str = "",
) -> str | None:
    for candidate_selector in (selector, FALLBACK_AUTHOR_SELECTOR):
        author = _extract_text(container, candidate_selector)
        if author:
            return author

    for meta_selector in FALLBACK_META_AUTHOR_SELECTORS:
        node = document.select_one(meta_selector)
        if node is None:
            continue
        author = clean_text(node.get("content"))
        if author:
            return author

    return None


def _looks_like_javascript_rendered_page(soup: BeautifulSoup, html: str) -> bool:
    visible_text = (clean_text(soup.get_text(" ", strip=True)) or "").casefold()
    if any(marker in visible_text for marker in JAVASCRIPT_REQUIRED_TEXT_MARKERS):
        return True

    script_count = len(soup.select("script"))
    has_app_root = any(soup.select_one(selector) is not None for selector in JAVASCRIPT_APP_ROOT_SELECTORS)
    has_bootstrap_marker = any(marker.casefold() in html.casefold() for marker in JAVASCRIPT_BOOTSTRAP_MARKERS)

    return len(visible_text) < 200 and script_count > 0 and (has_app_root or has_bootstrap_marker)
