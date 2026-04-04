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
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Ensure the project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.fetcher.common import build_request_headers  # noqa: E402
from src.fetcher.models import Source  # noqa: E402
from src.fetcher.registry import load_source_registry  # noqa: E402
from src.fetcher.scraper import (  # noqa: E402
    FALLBACK_ARTICLE_SELECTOR,
    FALLBACK_LINK_SELECTOR,
    FALLBACK_TITLE_SELECTOR,
    _looks_like_javascript_rendered_page,
)

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class SourceCheckResult:
    source: Source
    reachable: bool = False
    status_code: int | None = None
    error: str | None = None
    redirect_url: str | None = None
    content_length: int | None = None
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
) -> SourceCheckResult:
    result = SourceCheckResult(source=source)
    headers = build_request_headers(source.name, source.url)

    try:
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

        if source.method != "scrape":
            return result

        # Test selectors for scrape sources
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
            # Check both child elements and the container itself (for <a> containers)
            has_link = bool(container.select_one(link_sel))
            if not has_link and container.name == "a" and container.get("href"):
                has_link = True
            if has_link:
                links_found += 1
        result.titles_found = titles_found
        result.links_found = links_found

        if result.containers_found == 0:
            result.warnings.append("No article containers found with configured selectors")
        elif titles_found == 0:
            result.warnings.append(
                f"Found {result.containers_found} containers but 0 titles — title selector may be wrong"
            )
        elif links_found == 0:
            result.warnings.append(
                f"Found {result.containers_found} containers but 0 links — link selector may be wrong"
            )

        if _looks_like_javascript_rendered_page(soup, response.text):
            result.js_rendered = True
            result.warnings.append("Page appears to require JavaScript rendering")

    except httpx.TimeoutException:
        result.error = "Timeout"
    except httpx.ConnectError as exc:
        result.error = f"Connection error: {exc}"
    except httpx.HTTPError as exc:
        result.error = f"HTTP error: {exc}"
    except Exception as exc:
        result.error = f"Unexpected: {type(exc).__name__}: {exc}"

    return result


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

    async with httpx.AsyncClient(follow_redirects=True) as client:

        async def _check(source: Source) -> SourceCheckResult:
            async with semaphore:
                return await check_source(source, client, timeout, verbose)

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
        ok=sum(1 for r in results if r.reachable and not r.warnings),
        warnings=sum(1 for r in results if r.reachable and r.warnings),
        failed=sum(1 for r in results if not r.reachable),
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
            if not r.reachable:
                status = STATUS_SYMBOLS["fail"]
            elif r.warnings:
                status = STATUS_SYMBOLS["warn"]
            else:
                status = STATUS_SYMBOLS["ok"]

            tier_label = f"T{r.source.tier}"
            method_label = r.source.method.upper()
            print(f"  {status} [{tier_label}] [{method_label:6s}] {r.source.name}")

            if r.error:
                print(f"       Error: {r.error}")
            if r.redirect_url:
                print(f"       Redirected to: {r.redirect_url}")
            if r.source.method == "scrape" and r.reachable:
                print(
                    f"       Containers: {r.containers_found}  "
                    f"Titles: {r.titles_found}  "
                    f"Links: {r.links_found}"
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
            if not r.reachable:
                print(f"  {STATUS_SYMBOLS['fail']} {r.source.name}: {r.error}")
            elif r.warnings:
                for w in r.warnings:
                    print(f"  {STATUS_SYMBOLS['warn']} {r.source.name}: {w}")


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
