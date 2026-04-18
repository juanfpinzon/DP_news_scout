from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from src.fetcher.common import (
    DomainRateLimiter,
    RobotsPolicy,
    build_request_headers,
    clean_text,
    is_recent_enough,
    parse_datetime,
)
from src.analyzer.shared import _sanitize_prompt_text
from src.fetcher.models import RawArticle, Source
from src.fetcher.registry import load_source_registry
from src.utils.config import CONFIG_DIR, Settings
from src.utils.progress import emit_progress

BRAVE_NEWS_SEARCH_URL = "https://api.search.brave.com/res/v1/news/search"
DEFAULT_ALLOWLIST_PATH = CONFIG_DIR / "search_fallback_allowlist.yaml"
SEARCH_FALLBACK_DISCOVERY_METHOD = "search_fallback"
DEFAULT_AUTO_INCLUDE_CATEGORIES = ("trade_media", "mainstream", "global_news")
BRAVE_SEARCH_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
BRAVE_SEARCH_MAX_ATTEMPTS = 3
SEARCH_FALLBACK_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
SEARCH_FALLBACK_MAX_REDIRECTS = 2


class SearchFallbackError(RuntimeError):
    """Raised when the search fallback cannot be completed."""


@dataclass(slots=True, frozen=True)
class SearchFallbackPublisher:
    domain: str
    label: str
    group: str
    active: bool = True


@dataclass(slots=True, frozen=True)
class SearchFallbackAllowlist:
    publishers: dict[str, SearchFallbackPublisher]
    deny_domains: set[str]
    auto_include_source_categories: tuple[str, ...]


@dataclass(slots=True)
class SearchFallbackDiagnostics:
    query: str
    desired_results: int
    candidate_limit: int
    brave_results: int = 0
    accepted_articles: int = 0
    skipped_invalid_results: int = 0
    skipped_duplicate_urls: int = 0
    rejected_domain_not_allowed: int = 0
    rejected_source_domain_blocked: int = 0
    rejected_robots_disallowed: int = 0
    rejected_candidate_fetch_failed: int = 0
    rejected_stale: int = 0
    rejected_missing_title: int = 0


@dataclass(slots=True, frozen=True)
class _CandidateResponse:
    response: httpx.Response
    publisher: SearchFallbackPublisher
    url: str


async def search_fallback_articles(
    source: Source,
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    rate_limiter: DomainRateLimiter | None,
    robots_policy: RobotsPolicy | None,
    allow_robots_network_fallback: bool,
    now: datetime | None = None,
    logger: Any | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> list[RawArticle]:
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        raise SearchFallbackError("BRAVE_SEARCH_API_KEY is required for search fallback")

    allowlist = load_effective_search_allowlist()
    active_now = now or datetime.now(timezone.utc)
    desired_results = _resolve_result_limit(source=source, settings=settings)
    candidate_limit = max(6, min(10, desired_results * 4))
    query = _build_query(source)
    diagnostics = SearchFallbackDiagnostics(
        query=query,
        desired_results=desired_results,
        candidate_limit=candidate_limit,
    )
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    if rate_limiter is not None:
        await rate_limiter.wait(BRAVE_NEWS_SEARCH_URL)

    response = await _fetch_brave_search_response(
        source=source,
        client=client,
        headers=headers,
        params={"q": query, "count": candidate_limit},
        timeout_seconds=settings.search_fallback_timeout_seconds,
    )

    payload = response.json()
    results = payload.get("results")
    if not isinstance(results, list):
        raise SearchFallbackError("Brave search payload did not include a results list")
    diagnostics.brave_results = len(results)

    accepted: list[RawArticle] = []
    seen_urls: set[str] = set()
    source_domain = _hostname_from_url(source.url)

    for item in results:
        if len(accepted) >= desired_results:
            break
        if not isinstance(item, dict):
            diagnostics.skipped_invalid_results += 1
            continue

        candidate_url = item.get("url")
        if not isinstance(candidate_url, str) or not candidate_url.strip():
            diagnostics.skipped_invalid_results += 1
            continue
        candidate_url = candidate_url.strip()
        if candidate_url in seen_urls:
            diagnostics.skipped_duplicate_urls += 1
            continue

        publisher = resolve_allowed_publisher(candidate_url, allowlist=allowlist)
        if publisher is None:
            _record_search_fallback_rejection(
                diagnostics=diagnostics,
                logger=logger,
                source=source,
                candidate_url=candidate_url,
                reason="domain_not_allowed",
            )
            continue
        if _hostname_matches_domain(candidate_url, source_domain):
            _record_search_fallback_rejection(
                diagnostics=diagnostics,
                logger=logger,
                source=source,
                candidate_url=candidate_url,
                reason="source_domain_blocked",
            )
            continue

        article, rejection_reason = await _build_article_from_candidate(
            source=source,
            candidate=item,
            publisher=publisher,
            allowlist=allowlist,
            source_domain=source_domain,
            client=client,
            settings=settings,
            rate_limiter=rate_limiter,
            robots_policy=robots_policy,
            allow_robots_network_fallback=allow_robots_network_fallback,
            now=active_now,
        )
        if article is None:
            if rejection_reason is not None:
                _record_search_fallback_rejection(
                    diagnostics=diagnostics,
                    logger=logger,
                    source=source,
                    candidate_url=candidate_url,
                    reason=rejection_reason,
                )
            continue
        if article.url in seen_urls:
            diagnostics.skipped_duplicate_urls += 1
            continue

        accepted.append(article)
        seen_urls.add(candidate_url)
        seen_urls.add(article.url)

    diagnostics.accepted_articles = len(accepted)
    summary = _format_search_fallback_summary(diagnostics)
    _log_search_fallback_summary(
        logger,
        source=source,
        diagnostics=diagnostics,
        summary=summary,
    )
    emit_progress(
        progress_callback,
        f"Search fallback for {source.name}: {summary}",
    )
    return accepted


async def _fetch_brave_search_response(
    *,
    source: Source,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    params: dict[str, Any],
    timeout_seconds: float,
) -> httpx.Response:
    last_error: Exception | None = None

    for attempt in range(1, BRAVE_SEARCH_MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                BRAVE_NEWS_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            last_error = exc
            if attempt < BRAVE_SEARCH_MAX_ATTEMPTS and status_code in BRAVE_SEARCH_RETRYABLE_STATUS_CODES:
                await asyncio.sleep(0.5 * attempt)
                continue
            raise SearchFallbackError(
                f"Brave search request failed for {source.name}: HTTP {status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < BRAVE_SEARCH_MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
                continue
            raise SearchFallbackError(
                f"Brave search request failed for {source.name}: {type(exc).__name__}"
            ) from exc

    raise SearchFallbackError(f"Brave search request failed for {source.name}") from last_error


def load_effective_search_allowlist(
    *,
    path: Path | None = None,
    source_registry: list[Source] | None = None,
) -> SearchFallbackAllowlist:
    base = load_search_fallback_allowlist(path=path)
    publishers = dict(base.publishers)
    deny_domains = set(base.deny_domains)
    auto_categories = set(base.auto_include_source_categories)

    if auto_categories:
        registry_sources = source_registry or load_source_registry(active_only=False)
        for source in registry_sources:
            if source.category not in auto_categories:
                continue
            domain = _normalized_hostname(urlparse(source.url).netloc)
            if not domain or domain in publishers or domain in deny_domains:
                continue
            publishers[domain] = SearchFallbackPublisher(
                domain=domain,
                label=source.name,
                group=source.category,
                active=True,
            )

    return SearchFallbackAllowlist(
        publishers=publishers,
        deny_domains=deny_domains,
        auto_include_source_categories=base.auto_include_source_categories,
    )


def load_search_fallback_allowlist(
    *,
    path: Path | None = None,
) -> SearchFallbackAllowlist:
    config_path = path or DEFAULT_ALLOWLIST_PATH
    if not config_path.exists():
        raise SearchFallbackError(f"Missing search fallback allowlist config: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise SearchFallbackError("search fallback allowlist config must be a mapping")

    raw_publishers = payload.get("publishers", [])
    if not isinstance(raw_publishers, list):
        raise SearchFallbackError("search fallback allowlist publishers must be a list")

    publishers: dict[str, SearchFallbackPublisher] = {}
    for index, item in enumerate(raw_publishers, start=1):
        if not isinstance(item, dict):
            raise SearchFallbackError(f"publishers[{index}] must be a mapping")
        domain = _normalize_domain_value(item.get("domain"), field=f"publishers[{index}].domain")
        label = _normalize_label_value(item.get("label"), field=f"publishers[{index}].label")
        group = _normalize_label_value(item.get("group"), field=f"publishers[{index}].group")
        active = item.get("active", True)
        if not isinstance(active, bool):
            raise SearchFallbackError(f"publishers[{index}].active must be a boolean")
        publishers[domain] = SearchFallbackPublisher(
            domain=domain,
            label=label,
            group=group,
            active=active,
        )

    raw_deny_domains = payload.get("deny_domains", [])
    if not isinstance(raw_deny_domains, list):
        raise SearchFallbackError("search fallback allowlist deny_domains must be a list")
    deny_domains = {
        _normalize_domain_value(domain, field="deny_domains[]")
        for domain in raw_deny_domains
    }

    raw_auto_categories = payload.get(
        "auto_include_source_categories",
        list(DEFAULT_AUTO_INCLUDE_CATEGORIES),
    )
    if not isinstance(raw_auto_categories, list):
        raise SearchFallbackError("auto_include_source_categories must be a list")
    auto_categories = tuple(
        _normalize_label_value(category, field="auto_include_source_categories[]")
        for category in raw_auto_categories
    )

    return SearchFallbackAllowlist(
        publishers=publishers,
        deny_domains=deny_domains,
        auto_include_source_categories=auto_categories,
    )


def resolve_allowed_publisher(
    candidate_url: str,
    *,
    allowlist: SearchFallbackAllowlist,
) -> SearchFallbackPublisher | None:
    hostname = _hostname_from_url(candidate_url)
    if not hostname:
        return None
    if _matches_domains(hostname, allowlist.deny_domains):
        return None
    for domain, publisher in allowlist.publishers.items():
        if publisher.active and _hostname_matches_value(hostname, domain):
            return publisher
    return None


async def _build_article_from_candidate(
    *,
    source: Source,
    candidate: dict[str, Any],
    publisher: SearchFallbackPublisher,
    allowlist: SearchFallbackAllowlist,
    source_domain: str,
    client: httpx.AsyncClient,
    settings: Settings,
    rate_limiter: DomainRateLimiter | None,
    robots_policy: RobotsPolicy | None,
    allow_robots_network_fallback: bool,
    now: datetime,
) -> tuple[RawArticle | None, str | None]:
    candidate_url = str(candidate["url"]).strip()
    candidate_response, rejection_reason = await _fetch_candidate_response(
        source=source,
        candidate_url=candidate_url,
        publisher=publisher,
        allowlist=allowlist,
        source_domain=source_domain,
        client=client,
        rate_limiter=rate_limiter,
        robots_policy=robots_policy,
        allow_robots_network_fallback=allow_robots_network_fallback,
    )
    if candidate_response is None:
        return None, rejection_reason

    response = candidate_response.response
    final_url = candidate_response.url
    final_publisher = candidate_response.publisher

    soup = BeautifulSoup(response.text, "html.parser")
    published_at = _extract_document_date(soup) or parse_datetime(candidate.get("page_age"))
    if not is_recent_enough(published_at, now=now, lookback_hours=settings.rss_lookback_hours):
        return None, "stale"

    title = (
        _extract_meta_content(soup, "property", ("og:title",))
        or _extract_meta_content(soup, "name", ("twitter:title",))
        or clean_text(soup.title.get_text(" ", strip=True) if soup.title else None)
        or clean_text(candidate.get("title"))
    )
    if not title:
        return None, "missing_title"

    summary = (
        _extract_meta_content(soup, "name", ("description",))
        or _extract_meta_content(soup, "property", ("og:description",))
        or clean_text(candidate.get("description"))
    )
    author = _extract_meta_content(soup, "name", ("author", "article:author"))

    return (
        RawArticle(
            url=final_url,
            title=_sanitize_prompt_text(title) or title,
            source=final_publisher.label,
            source_url=f"https://{final_publisher.domain}/",
            category=_publisher_category(final_publisher),
            published_at=published_at.isoformat() if published_at is not None else None,
            fetched_at=now.isoformat(),
            summary=_sanitize_prompt_text(summary) or summary,
            author=_sanitize_prompt_text(author) if author is not None else None,
            origin_source=source.name,
            discovery_method=SEARCH_FALLBACK_DISCOVERY_METHOD,
        ),
        None,
    )


async def _fetch_candidate_response(
    *,
    source: Source,
    candidate_url: str,
    publisher: SearchFallbackPublisher,
    allowlist: SearchFallbackAllowlist,
    source_domain: str,
    client: httpx.AsyncClient,
    rate_limiter: DomainRateLimiter | None,
    robots_policy: RobotsPolicy | None,
    allow_robots_network_fallback: bool,
) -> tuple[_CandidateResponse | None, str | None]:
    current_url = candidate_url
    current_publisher = publisher
    redirect_count = 0
    request_headers = build_request_headers(source.name, candidate_url)

    if rate_limiter is not None:
        await rate_limiter.wait(candidate_url)

    while True:
        if robots_policy is not None:
            allowed = await robots_policy.allows(
                client=client,
                url=current_url,
                user_agent=request_headers["User-Agent"],
                allow_network_fallback=allow_robots_network_fallback,
            )
            if not allowed:
                return None, "robots_disallowed"

        try:
            response = await client.get(
                current_url,
                headers=request_headers,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return None, "candidate_fetch_failed"

        if response.status_code in SEARCH_FALLBACK_REDIRECT_STATUS_CODES:
            if redirect_count >= SEARCH_FALLBACK_MAX_REDIRECTS:
                return None, "candidate_fetch_failed"

            redirect_url = _resolve_redirect_url(response)
            if redirect_url is None:
                return None, "candidate_fetch_failed"

            redirect_publisher = resolve_allowed_publisher(
                redirect_url,
                allowlist=allowlist,
            )
            if redirect_publisher is None:
                return None, "domain_not_allowed"
            if _hostname_matches_domain(redirect_url, source_domain):
                return None, "source_domain_blocked"

            current_url = redirect_url
            current_publisher = redirect_publisher
            redirect_count += 1
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPError:
            return None, "candidate_fetch_failed"

        return (
            _CandidateResponse(
                response=response,
                publisher=current_publisher,
                url=str(response.url),
            ),
            None,
        )


def _extract_meta_content(
    soup: BeautifulSoup,
    attribute_name: str,
    attribute_values: tuple[str, ...],
) -> str | None:
    for attribute_value in attribute_values:
        node = soup.find("meta", attrs={attribute_name: attribute_value})
        if node is None:
            continue
        content = clean_text(node.get("content"))
        if content:
            return content
    return None


def _resolve_redirect_url(response: httpx.Response) -> str | None:
    location = clean_text(response.headers.get("Location"))
    if not location:
        return None
    return urljoin(str(response.url), location)


def _extract_document_date(soup: BeautifulSoup) -> datetime | None:
    for attribute_name, attribute_values in (
        ("property", ("article:published_time", "og:updated_time")),
        (
            "name",
            (
                "article:published_time",
                "publish-date",
                "pubdate",
                "date",
                "dc.date",
                "parsely-pub-date",
            ),
        ),
    ):
        for attribute_value in attribute_values:
            node = soup.find("meta", attrs={attribute_name: attribute_value})
            if node is None:
                continue
            parsed = parse_datetime(node.get("content"))
            if parsed is not None:
                return parsed

    time_node = soup.find("time")
    if time_node is not None:
        parsed = parse_datetime(time_node.get("datetime") or time_node.get_text(" ", strip=True))
        if parsed is not None:
            return parsed

    return None


def _publisher_category(publisher: SearchFallbackPublisher) -> str:
    normalized_group = publisher.group.strip().casefold()
    return normalized_group or "trade_media"


def _build_query(source: Source) -> str:
    if source.fallback_search.query:
        return source.fallback_search.query
    return f"\"{source.name}\""


def _resolve_result_limit(*, source: Source, settings: Settings) -> int:
    requested = source.fallback_search.max_results or settings.search_fallback_max_results_per_source
    return max(1, min(3, requested))


def _hostname_from_url(url: str) -> str:
    return _normalized_hostname(urlparse(url).netloc)


def _normalized_hostname(hostname: str) -> str:
    return hostname.strip().lower().removeprefix("www.")


def _normalize_domain_value(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchFallbackError(f"{field} must be a non-empty string")
    return _normalized_hostname(value)


def _normalize_label_value(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SearchFallbackError(f"{field} must be a non-empty string")
    return value.strip()


def _matches_domains(hostname: str, domains: set[str]) -> bool:
    return any(_hostname_matches_value(hostname, domain) for domain in domains)


def _hostname_matches_domain(candidate_url: str, domain: str) -> bool:
    hostname = _hostname_from_url(candidate_url)
    return _hostname_matches_value(hostname, domain)


def _hostname_matches_value(hostname: str, domain: str) -> bool:
    normalized_domain = _normalized_hostname(domain)
    if not hostname or not normalized_domain:
        return False
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def _record_search_fallback_rejection(
    *,
    diagnostics: SearchFallbackDiagnostics,
    logger: Any | None,
    source: Source,
    candidate_url: str,
    reason: str,
) -> None:
    if reason == "domain_not_allowed":
        diagnostics.rejected_domain_not_allowed += 1
    elif reason == "source_domain_blocked":
        diagnostics.rejected_source_domain_blocked += 1
    elif reason == "robots_disallowed":
        diagnostics.rejected_robots_disallowed += 1
    elif reason == "candidate_fetch_failed":
        diagnostics.rejected_candidate_fetch_failed += 1
    elif reason == "stale":
        diagnostics.rejected_stale += 1
    elif reason == "missing_title":
        diagnostics.rejected_missing_title += 1

    _log_search_fallback_rejection(
        logger=logger,
        source=source,
        candidate_url=candidate_url,
        reason=reason,
    )


def _log_search_fallback_rejection(
    *,
    logger: Any | None,
    source: Source,
    candidate_url: str,
    reason: str,
) -> None:
    if logger is None:
        return
    logger.info(
        "source_search_fallback_candidate_rejected",
        source=source.name,
        source_url=source.url,
        candidate_url=candidate_url,
        reason=reason,
    )


def _log_search_fallback_summary(
    logger: Any | None,
    *,
    source: Source,
    diagnostics: SearchFallbackDiagnostics,
    summary: str,
) -> None:
    if logger is None:
        return

    logger.info(
        "source_search_fallback_complete",
        source=source.name,
        source_url=source.url,
        query=diagnostics.query,
        desired_results=diagnostics.desired_results,
        candidate_limit=diagnostics.candidate_limit,
        brave_results=diagnostics.brave_results,
        accepted_articles=diagnostics.accepted_articles,
        skipped_invalid_results=diagnostics.skipped_invalid_results,
        skipped_duplicate_urls=diagnostics.skipped_duplicate_urls,
        rejected_domain_not_allowed=diagnostics.rejected_domain_not_allowed,
        rejected_source_domain_blocked=diagnostics.rejected_source_domain_blocked,
        rejected_robots_disallowed=diagnostics.rejected_robots_disallowed,
        rejected_candidate_fetch_failed=diagnostics.rejected_candidate_fetch_failed,
        rejected_stale=diagnostics.rejected_stale,
        rejected_missing_title=diagnostics.rejected_missing_title,
        summary=summary,
    )


def _format_search_fallback_summary(diagnostics: SearchFallbackDiagnostics) -> str:
    parts = [f"Brave returned {diagnostics.brave_results} result{'s' if diagnostics.brave_results != 1 else ''}"]

    if diagnostics.accepted_articles:
        parts.append(
            f"accepted {diagnostics.accepted_articles} article{'s' if diagnostics.accepted_articles != 1 else ''}"
        )

    for count, label in (
        (diagnostics.rejected_domain_not_allowed, "blocked by allowlist"),
        (diagnostics.rejected_source_domain_blocked, "from the original source domain"),
        (diagnostics.rejected_robots_disallowed, "blocked by robots.txt"),
        (diagnostics.rejected_candidate_fetch_failed, "article fetch failed"),
        (diagnostics.rejected_stale, "stale"),
        (diagnostics.rejected_missing_title, "missing title"),
        (diagnostics.skipped_duplicate_urls, "duplicate"),
        (diagnostics.skipped_invalid_results, "invalid"),
    ):
        if count:
            parts.append(f"{count} {label}")

    return "; ".join(parts) + "."
