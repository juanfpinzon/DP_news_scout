from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from email.utils import parsedate_to_datetime
from html import unescape
from time import monotonic
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zlib import adler32

import httpx
from bs4 import BeautifulSoup

USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
)

TRACKABLE_CONTENT_MARKERS = (
    "/article",
    "/articles",
    "/blog",
    "/insight",
    "/insights",
    "/news",
    "/post",
    "/posts",
    "/resource",
    "/resources",
    "/story",
    "/stories",
)

ROBOTS_TXT_ACCEPT_HEADER = "text/plain,*/*;q=0.1"
ROBOTS_TXT_TIMEOUT_SECONDS = 15.0
ROBOTS_CACHE_TTL_SECONDS = 900.0
ROBOTS_CACHE_MAX_ENTRIES = 256


def build_request_headers(source_name: str, url: str) -> dict[str, str]:
    rotation_key = adler32(f"{source_name}:{url}".encode("utf-8"))
    user_agent = USER_AGENTS[rotation_key % len(USER_AGENTS)]
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": user_agent,
    }


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unescape(value).split())
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    return normalized or None


def strip_html(value: str | None) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return clean_text(text)


def coerce_absolute_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    return urljoin(base_url, value.strip())


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)

    text = clean_text(str(value))
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        pass

    try:
        return _as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass

    for fmt in (
        "%Y-%m-%d",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d.%m.%y",
        "%d.%m.%Y",
    ):
        try:
            return _as_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue

    return None


def is_recent_enough(
    published_at: datetime | None,
    *,
    now: datetime,
    lookback_hours: int,
) -> bool:
    if published_at is None:
        return True
    cutoff = now - timedelta(hours=lookback_hours)
    return published_at >= cutoff


def looks_like_content_url(base_domain: str, href: str) -> bool:
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return False
    candidate_domain = parsed.netloc.lower().removeprefix("www.")
    if candidate_domain and base_domain and not candidate_domain.endswith(base_domain):
        return False
    path = parsed.path.casefold()
    return any(marker in path for marker in TRACKABLE_CONTENT_MARKERS)


def same_domain(base_url: str, url: str) -> bool:
    base_domain = urlparse(base_url).netloc.lower().removeprefix("www.")
    candidate_domain = urlparse(url).netloc.lower().removeprefix("www.")
    return candidate_domain == "" or candidate_domain.endswith(base_domain)


@asynccontextmanager
async def managed_async_client(
    client: httpx.AsyncClient | None,
    *,
    timeout_seconds: float,
) -> httpx.AsyncClient:
    if client is not None:
        yield client
        return

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds),
    ) as managed_client:
        yield managed_client


class DomainRateLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._last_called: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str) -> None:
        if self._interval_seconds <= 0:
            return

        domain = urlparse(url).netloc.lower()
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = monotonic()
            last_called = self._last_called.get(domain)
            if last_called is not None:
                remaining = self._interval_seconds - (now - last_called)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_called[domain] = monotonic()


class _RobotsFetchState(Enum):
    PARSED = "parsed"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class _RobotsFetchResult:
    state: _RobotsFetchState
    parser: robotparser.RobotFileParser | None = None


@dataclass(slots=True)
class _RobotsCacheEntry:
    result: _RobotsFetchResult
    expires_at: float


class RobotsPolicy:
    def __init__(
        self,
        *,
        cache_ttl_seconds: float = ROBOTS_CACHE_TTL_SECONDS,
        max_cache_entries: int = ROBOTS_CACHE_MAX_ENTRIES,
    ) -> None:
        self._cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._max_cache_entries = max(1, max_cache_entries)
        self._parsers: OrderedDict[str, _RobotsCacheEntry] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def allows(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        user_agent: str,
        allow_network_fallback: bool = False,
    ) -> bool:
        parsed_url = urlparse(url)
        robots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/robots.txt"
        lock = self._locks.setdefault(robots_url, asyncio.Lock())

        async with lock:
            entry = self._get_cached_entry(robots_url)
            if entry is None:
                result = await self._fetch_parser(
                    client=client,
                    robots_url=robots_url,
                    user_agent=user_agent,
                    allow_network_fallback=allow_network_fallback,
                )
                entry = self._store_cached_entry(robots_url, result)

            if entry.result.state is _RobotsFetchState.MISSING:
                return True
            if entry.result.state is _RobotsFetchState.UNKNOWN or entry.result.parser is None:
                return False
            return entry.result.parser.can_fetch(user_agent, url)

    def _get_cached_entry(self, robots_url: str) -> _RobotsCacheEntry | None:
        entry = self._parsers.get(robots_url)
        if entry is None:
            return None
        if entry.expires_at <= monotonic():
            self._parsers.pop(robots_url, None)
            return None
        self._parsers.move_to_end(robots_url)
        return entry

    def _store_cached_entry(
        self,
        robots_url: str,
        result: _RobotsFetchResult,
    ) -> _RobotsCacheEntry:
        entry = _RobotsCacheEntry(
            result=result,
            expires_at=monotonic() + self._cache_ttl_seconds,
        )
        self._parsers[robots_url] = entry
        self._parsers.move_to_end(robots_url)

        while len(self._parsers) > self._max_cache_entries:
            self._parsers.popitem(last=False)

        return entry

    async def _fetch_parser(
        self,
        *,
        client: httpx.AsyncClient,
        robots_url: str,
        user_agent: str,
        allow_network_fallback: bool,
    ) -> _RobotsFetchResult:
        try:
            response = await client.get(
                robots_url,
                headers={"User-Agent": user_agent, "Accept": ROBOTS_TXT_ACCEPT_HEADER},
            )
        except httpx.HTTPError:
            return _RobotsFetchResult(state=_RobotsFetchState.UNKNOWN)

        if response.status_code in {401, 403}:
            if not allow_network_fallback:
                return _RobotsFetchResult(
                    state=_RobotsFetchState.PARSED,
                    parser=_disallow_all_parser(),
                )

            fallback_status, fallback_body = await asyncio.to_thread(
                _fetch_robots_txt_with_urllib,
                robots_url=robots_url,
                user_agent=user_agent,
            )
            if fallback_status is not None and 200 <= fallback_status < 300 and fallback_body is not None:
                return _RobotsFetchResult(
                    state=_RobotsFetchState.PARSED,
                    parser=_parse_robots_body(robots_url=robots_url, body=fallback_body),
                )
            if fallback_status in {404, 410}:
                return _RobotsFetchResult(state=_RobotsFetchState.MISSING)
            return _RobotsFetchResult(
                state=_RobotsFetchState.PARSED,
                parser=_disallow_all_parser(),
            )

        if 200 <= response.status_code < 300:
            return _RobotsFetchResult(
                state=_RobotsFetchState.PARSED,
                parser=_parse_robots_body(robots_url=robots_url, body=response.text),
            )

        if response.status_code in {404, 410}:
            return _RobotsFetchResult(state=_RobotsFetchState.MISSING)

        return _RobotsFetchResult(state=_RobotsFetchState.UNKNOWN)


def _fetch_robots_txt_with_urllib(*, robots_url: str, user_agent: str) -> tuple[int | None, str | None]:
    request = Request(
        robots_url,
        headers={
            "User-Agent": user_agent,
            "Accept": ROBOTS_TXT_ACCEPT_HEADER,
        },
    )
    try:
        with urlopen(request, timeout=ROBOTS_TXT_TIMEOUT_SECONDS) as response:
            return response.status, _decode_robots_body(response.read(), response.headers)
    except HTTPError as exc:
        return exc.code, _decode_robots_body(exc.read(), exc.headers)
    except (OSError, URLError):
        return None, None


def _decode_robots_body(body: bytes, headers: object) -> str:
    charset_getter = getattr(headers, "get_content_charset", None)
    charset = charset_getter() if callable(charset_getter) else None
    return body.decode(charset or "utf-8", errors="replace")


def _parse_robots_body(*, robots_url: str, body: str) -> robotparser.RobotFileParser:
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(body.splitlines())
    return parser


def _disallow_all_parser() -> robotparser.RobotFileParser:
    parser = robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /"])
    return parser


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
