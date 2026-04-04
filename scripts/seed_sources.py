"""Validate all source URLs and selectors in config/sources.yaml.

Hits every source URL, checks reachability, tests CSS selectors for scrape
sources, and prints a health report. Intended to be run manually after adding
or editing sources.

Usage:
    python scripts/seed_sources.py [--timeout 15] [--verbose] [--include-inactive]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup

# Ensure the project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.fetcher.common import DomainRateLimiter, RobotsPolicy, build_request_headers  # noqa: E402
from src.fetcher.models import Source  # noqa: E402
from src.fetcher.registry import load_source_registry  # noqa: E402
from src.fetcher.scraper import (  # noqa: E402
    FALLBACK_ARTICLE_SELECTOR,
    FALLBACK_LINK_SELECTOR,
    FALLBACK_TITLE_SELECTOR,
    _fallback_anchor_scan,
    _looks_like_javascript_rendered_page,
    _parse_article_containers,
)

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

VALIDATION_LOOKBACK_HOURS = 24 * 365


@dataclass
class SourceCheckResult:
    source: Source
    reachable: bool = False
    status_code: int | None = None
    error: str | None = None
    redirect_url: str | None = None
    content_length: int | None = None
    articles_found: int = 0
    feed_format: str | None = None
    rss_entries_found: int = 0
    containers_found: int = 0
    titles_found: int = 0
    links_found: int = 0
    js_rendered: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationCounts:
    ok: int
    warnings: int
    failed: int


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

async def check_source(
    source: Source,
    client: httpx.AsyncClient,
    timeout: float,
    verbose: bool,
    *,
    rate_limiter: DomainRateLimiter | None = None,
    robots_policy: RobotsPolicy | None = None,
) -> SourceCheckResult:
    result = SourceCheckResult(source=source)
    headers = build_request_headers(source.name, source.url)

    try:
        if robots_policy is not None:
            allowed = await robots_policy.allows(
                client=client,
                url=source.url,
                user_agent=headers["User-Agent"],
            )
            if not allowed:
                result.error = "Blocked by robots.txt"
                return result

        if rate_limiter is not None:
            await rate_limiter.wait(source.url)

        response = await client.get(
            source.url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        result.status_code = response.status_code
        result.content_length = len(response.content)

        if str(response.url) != source.url:
            result.redirect_url = str(response.url)

        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            return result

        result.reachable = True

        if source.method == "rss":
            _check_rss_source(source=source, response=response, result=result)
            return result

        _check_scrape_source(source=source, response=response, result=result)

    except httpx.TimeoutException:
        result.error = "Timeout"
    except httpx.ConnectError as exc:
        result.error = f"Connection error: {exc}"
    except httpx.HTTPError as exc:
        result.error = f"HTTP error: {exc}"
    except Exception as exc:
        result.error = f"Unexpected: {type(exc).__name__}: {exc}"

    return result


def _check_rss_source(
    *,
    source: Source,
    response: httpx.Response,
    result: SourceCheckResult,
) -> None:
    parsed_feed = feedparser.parse(response.content)
    result.feed_format = getattr(parsed_feed, "version", None) or None
    entries = getattr(parsed_feed, "entries", [])
    result.rss_entries_found = len(entries)
    result.articles_found = result.rss_entries_found

    bozo_exception = getattr(parsed_feed, "bozo_exception", None)
    if bozo_exception is not None and result.rss_entries_found == 0:
        result.error = (
            f"Malformed feed for {source.name}: "
            f"{type(bozo_exception).__name__}: {bozo_exception}"
        )
        return

    if result.feed_format is None:
        result.warnings.append("RSS parser did not detect a valid feed format")
    if result.rss_entries_found == 0:
        result.warnings.append("Feed is reachable but returned 0 entries")


def _check_scrape_source(
    *,
    source: Source,
    response: httpx.Response,
    result: SourceCheckResult,
) -> None:
    soup = BeautifulSoup(response.text, "html.parser")

    article_sel = source.selectors.get("article", FALLBACK_ARTICLE_SELECTOR)
    title_sel = source.selectors.get("title", FALLBACK_TITLE_SELECTOR)
    link_sel = source.selectors.get("link", FALLBACK_LINK_SELECTOR)

    containers = soup.select(article_sel)
    result.containers_found = len(containers)

    titles_found = 0
    links_found = 0
    for container in containers[:20]:
        if container.select_one(title_sel):
            titles_found += 1
        has_link = bool(container.select_one(link_sel))
        if not has_link and container.name == "a" and container.get("href"):
            has_link = True
        if has_link:
            links_found += 1
    result.titles_found = titles_found
    result.links_found = links_found

    extracted_articles = _parse_article_containers(
        soup=soup,
        source=source,
        now=datetime.now(timezone.utc),
        lookback_hours=VALIDATION_LOOKBACK_HOURS,
        max_articles=20,
    )
    if not extracted_articles:
        extracted_articles = _fallback_anchor_scan(
            soup=soup,
            source=source,
            now=datetime.now(timezone.utc),
            lookback_hours=VALIDATION_LOOKBACK_HOURS,
            max_articles=20,
        )
    result.articles_found = len(extracted_articles)

    if result.containers_found == 0:
        result.warnings.append("No article containers found with configured selectors")
    elif titles_found == 0:
        result.warnings.append(
            f"Found {result.containers_found} containers but 0 titles; title selector may be wrong"
        )
    elif links_found == 0:
        result.warnings.append(
            f"Found {result.containers_found} containers but 0 links; link selector may be wrong"
        )

    if result.articles_found == 0:
        result.warnings.append("Scraper did not extract any articles from the page")

    if _looks_like_javascript_rendered_page(soup, response.text):
        result.js_rendered = True
        result.warnings.append("Page appears to require JavaScript rendering")


async def validate_all(
    timeout: float,
    verbose: bool,
    *,
    include_inactive: bool = False,
) -> list[SourceCheckResult]:
    sources = load_source_registry(active_only=not include_inactive)
    scope_label = "all" if include_inactive else "active"
    print(f"Loaded {len(sources)} {scope_label} sources from sources.yaml\n")

    results: list[SourceCheckResult] = []
    semaphore = asyncio.Semaphore(5)
    rate_limiter = DomainRateLimiter(1.0)
    robots_policy = RobotsPolicy()

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
    ) as client:

        async def _check(source: Source) -> SourceCheckResult:
            async with semaphore:
                return await check_source(
                    source,
                    client,
                    timeout,
                    verbose,
                    rate_limiter=rate_limiter,
                    robots_policy=robots_policy,
                )

        tasks = [_check(s) for s in sources]
        results = await asyncio.gather(*tasks)

    return list(results)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

STATUS_SYMBOLS = {
    "ok": "\u2705",       # green check
    "warn": "\u26a0\ufe0f",  # warning
    "fail": "\u274c",     # red X
}


def summarize_results(results: list[SourceCheckResult]) -> ValidationCounts:
    return ValidationCounts(
        ok=sum(1 for r in results if _classify_result(r) == "ok"),
        warnings=sum(1 for r in results if _classify_result(r) == "warn"),
        failed=sum(1 for r in results if _classify_result(r) == "fail"),
    )


def print_report(results: list[SourceCheckResult], verbose: bool) -> None:
    # Group by category
    by_category: dict[str, list[SourceCheckResult]] = {}
    for r in results:
        by_category.setdefault(r.source.category, []).append(r)

    counts = summarize_results(results)

    print("=" * 72)
    print("SOURCE HEALTH REPORT")
    print("=" * 72)

    for category in sorted(by_category):
        cat_results = sorted(by_category[category], key=lambda r: (r.source.tier, r.source.name))
        print(f"\n--- {category.upper()} ---")

        for r in cat_results:
            status = STATUS_SYMBOLS[_classify_result(r)]

            tier_label = f"T{r.source.tier}"
            method_label = r.source.method.upper()
            print(f"  {status} [{tier_label}] [{method_label:6s}] {r.source.name}")

            if r.error:
                print(f"       Error: {r.error}")
            if r.redirect_url:
                print(f"       Redirected to: {r.redirect_url}")
            if r.source.method == "rss" and r.reachable:
                feed_label = r.feed_format or "unknown"
                print(
                    f"       Feed: {feed_label}  "
                    f"Entries: {r.rss_entries_found}  "
                    f"Articles: {r.articles_found}"
                )
            if r.source.method == "scrape" and r.reachable:
                print(
                    f"       Containers: {r.containers_found}  "
                    f"Titles: {r.titles_found}  "
                    f"Links: {r.links_found}  "
                    f"Articles: {r.articles_found}"
                )
            for w in r.warnings:
                print(f"       ⚠ {w}")

            if verbose and r.reachable:
                print(f"       HTTP {r.status_code}, {r.content_length} bytes")

    print()
    print("-" * 72)
    print(f"Total: {len(results)} sources  |  "
          f"{STATUS_SYMBOLS['ok']} OK: {counts.ok}  |  "
          f"{STATUS_SYMBOLS['warn']} Warnings: {counts.warnings}  |  "
          f"{STATUS_SYMBOLS['fail']} Failed: {counts.failed}")
    print("-" * 72)

    if counts.failed or counts.warnings:
        print("\nSources needing attention:")
        for r in results:
            if _classify_result(r) == "fail":
                print(f"  {STATUS_SYMBOLS['fail']} {r.source.name}: {r.error}")
            elif _classify_result(r) == "warn":
                for w in r.warnings:
                    print(f"  {STATUS_SYMBOLS['warn']} {r.source.name}: {w}")


def _classify_result(result: SourceCheckResult) -> str:
    if result.error is not None or not result.reachable:
        return "fail"
    if result.warnings:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate sources.yaml URLs and selectors")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--verbose", action="store_true", help="Show extra details")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Validate inactive sources too instead of only the active registry",
    )
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="Exit successfully even when warnings are present",
    )
    args = parser.parse_args()

    results = asyncio.run(
        validate_all(
            timeout=args.timeout,
            verbose=args.verbose,
            include_inactive=args.include_inactive,
        )
    )
    print_report(results, verbose=args.verbose)

    counts = summarize_results(results)
    has_error = counts.failed > 0 or (counts.warnings > 0 and not args.allow_warnings)
    sys.exit(1 if has_error else 0)


if __name__ == "__main__":
    main()
