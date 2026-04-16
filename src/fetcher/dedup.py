from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.fetcher.models import RawArticle
from src.storage.db import get_recent_urls

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "oly_anon_id",
    "oly_enc_id",
    "ref",
    "ref_src",
    "source",
    "s_cid",
    "trk",
}

TRACKING_PREFIXES = ("utm_",)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower().removeprefix("www.")
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMS
        and not key.casefold().startswith(TRACKING_PREFIXES)
    ]
    normalized_path = parsed.path.rstrip("/") or "/"

    return urlunparse(
        (
            scheme,
            netloc,
            normalized_path,
            "",
            urlencode(sorted(filtered_query)),
            "",
        )
    )


def deduplicate_articles(
    articles: list[RawArticle],
    recent_urls: set[str] | None = None,
    *,
    database_path: str | None = None,
    dedup_window_days: int = 7,
    use_database_seen_urls: bool = True,
    now: datetime | None = None,
) -> list[RawArticle]:
    seen_urls: set[str] = set()
    if database_path is not None and use_database_seen_urls:
        seen_urls.update(
            load_recent_seen_urls(
                database_path,
                days=dedup_window_days,
                now=now,
            )
        )
    if recent_urls is not None:
        seen_urls.update(normalize_url(url) for url in recent_urls)

    deduplicated: list[RawArticle] = []

    for article in articles:
        normalized_url = normalize_url(article.url)
        if normalized_url in seen_urls:
            continue

        deduplicated.append(replace(article, url=normalized_url))
        seen_urls.add(normalized_url)

    return deduplicated


def load_recent_seen_urls(
    database_path: str,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> set[str]:
    return {
        normalize_url(url)
        for url in get_recent_urls(database_path, days=days, now=now)
    }
