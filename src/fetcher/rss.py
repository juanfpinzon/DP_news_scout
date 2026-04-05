from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from src.fetcher.common import (
    DomainRateLimiter,
    RobotsPolicy,
    build_request_headers,
    clean_text,
    coerce_absolute_url,
    is_recent_enough,
    managed_async_client,
    parse_datetime,
    strip_html,
)
from src.fetcher.models import RawArticle, Source


class RSSFetchError(RuntimeError):
    """Raised when an RSS or Atom feed cannot be fetched or parsed."""


async def fetch_rss(
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
    allow_robots_network_fallback = client is None
    async with managed_async_client(client, timeout_seconds=timeout_seconds) as active_client:
        if robots_policy is not None:
            allowed = await robots_policy.allows(
                client=active_client,
                url=source.url,
                user_agent=headers["User-Agent"],
                allow_network_fallback=allow_robots_network_fallback,
            )
            if not allowed:
                raise PermissionError(f"robots.txt disallows fetching {source.url}")

        if rate_limiter is not None:
            await rate_limiter.wait(source.url)

        try:
            response = await active_client.get(source.url, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RSSFetchError(f"Timed out fetching feed for {source.name}: {source.url}") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise RSSFetchError(
                f"Feed returned HTTP {status_code} for {source.name}: {source.url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RSSFetchError(f"Failed to fetch feed for {source.name}: {source.url}") from exc

    try:
        parsed_feed = feedparser.parse(response.content)
    except Exception as exc:
        raise RSSFetchError(f"Failed to parse feed for {source.name}: {source.url}") from exc

    if parsed_feed.bozo and not parsed_feed.entries:
        raise RSSFetchError(f"Malformed feed for {source.name}: {source.url}") from parsed_feed.bozo_exception

    articles: list[RawArticle] = []
    seen_urls: set[str] = set()
    fetched_at = active_now.isoformat()

    for entry in parsed_feed.entries:
        article_url = _extract_entry_url(source.url, entry)
        title = clean_text(entry.get("title"))
        if not article_url or not title or article_url in seen_urls:
            continue

        published_at = _parse_feed_date(entry)
        if not is_recent_enough(
            published_at,
            now=active_now,
            lookback_hours=lookback_hours,
        ):
            continue

        articles.append(
            RawArticle(
                url=article_url,
                title=title,
                source=source.name,
                source_url=source.url,
                category=source.category,
                published_at=published_at.isoformat() if published_at else None,
                fetched_at=fetched_at,
                summary=_extract_summary(entry),
                author=_extract_author(entry),
            )
        )
        seen_urls.add(article_url)

        if len(articles) >= max_articles:
            break

    return articles


def _extract_entry_url(base_url: str, entry: feedparser.FeedParserDict) -> str | None:
    direct_link = coerce_absolute_url(base_url, clean_text(entry.get("link")))
    if direct_link:
        return direct_link

    links = entry.get("links")
    if not isinstance(links, list):
        return None

    for link in links:
        if not isinstance(link, dict):
            continue
        href = coerce_absolute_url(base_url, clean_text(link.get("href")))
        if href:
            return href

    return None


def _extract_summary(entry: feedparser.FeedParserDict) -> str | None:
    for field_name in ("summary", "description", "subtitle"):
        summary = strip_html(entry.get(field_name))
        if summary:
            return summary

    content = entry.get("content")
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        summary = strip_html(block.get("value"))
        if summary:
            return summary

    return None


def _extract_author(entry: feedparser.FeedParserDict) -> str | None:
    for field_name in ("author", "dc_creator", "creator"):
        author = clean_text(entry.get(field_name))
        if author:
            return author

    authors = entry.get("authors")
    if not isinstance(authors, list):
        return None

    for author in authors:
        if not isinstance(author, dict):
            continue
        for field_name in ("name", "email"):
            value = clean_text(author.get(field_name))
            if value:
                return value

    return None


def _parse_feed_date(entry: feedparser.FeedParserDict) -> datetime | None:
    for field_name in ("published", "updated", "created"):
        parsed = parse_datetime(entry.get(field_name))
        if parsed is not None:
            return parsed

    for field_name in ("published_detail", "updated_detail", "created_detail"):
        parsed = _parse_detail_date(entry.get(field_name))
        if parsed is not None:
            return parsed

    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field_name)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)

    return None


def _parse_detail_date(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    return parse_datetime(value.get("value"))
