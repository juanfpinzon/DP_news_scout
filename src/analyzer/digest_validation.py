from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.analyzer.relevance import ScoredArticle


class DigestCompositionError(ValueError):
    """Raised when the LLM returns an invalid digest payload."""


@dataclass(slots=True)
class DigestItem:
    url: str
    headline: str
    summary: str
    why_it_matters: str
    source: str
    date: str


def parse_digest_item_list(
    value: Any,
    *,
    field_name: str,
    article_lookup: dict[str, ScoredArticle],
    article_urls: set[str],
    used_urls: set[str],
) -> list[DigestItem]:
    if not isinstance(value, list):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a list")

    return [
        parse_digest_item(
            item,
            field_name=f"{field_name}[{index}]",
            article_lookup=article_lookup,
            article_urls=article_urls,
            used_urls=used_urls,
        )
        for index, item in enumerate(value)
    ]


def parse_digest_item(
    value: Any,
    *,
    field_name: str,
    article_lookup: dict[str, ScoredArticle],
    article_urls: set[str],
    used_urls: set[str],
) -> DigestItem:
    if not isinstance(value, dict):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be an object")

    url = _validate_digest_url(
        _require_string(value.get("url"), f"{field_name}.url"),
        field_name=field_name,
        article_urls=article_urls,
        used_urls=used_urls,
    )
    article = article_lookup[url]

    return DigestItem(
        url=url,
        headline=_require_string(value.get("headline"), f"{field_name}.headline"),
        summary=_require_string(value.get("summary"), f"{field_name}.summary"),
        why_it_matters=_require_string(
            value.get("why_it_matters"),
            f"{field_name}.why_it_matters",
        ),
        source=resolve_digest_source(
            value.get("source"),
            field_name=f"{field_name}.source",
            article=article,
        ),
        date=_resolve_digest_date(
            value.get("date"),
            article=article,
        ),
    )


def _validate_digest_url(
    url: str,
    *,
    field_name: str,
    article_urls: set[str],
    used_urls: set[str],
) -> str:
    resolved_url = resolve_digest_url(
        url,
        field_name=field_name,
        article_urls=article_urls,
    )
    if resolved_url in used_urls:
        raise DigestCompositionError(f"Digest payload reuses article URL across sections: {resolved_url}")
    used_urls.add(resolved_url)
    return resolved_url


def resolve_digest_url(
    url: str,
    *,
    field_name: str,
    article_urls: set[str],
) -> str:
    normalized_url = _sanitize_digest_url(url)
    if normalized_url in article_urls:
        return normalized_url

    canonical_candidates = _find_canonical_url_matches(normalized_url, article_urls)
    if len(canonical_candidates) == 1:
        return canonical_candidates[0]

    truncated_candidates = _find_safe_truncated_url_matches(
        normalized_url,
        article_urls,
    )
    if len(truncated_candidates) == 1:
        return truncated_candidates[0]

    brand_qualified_candidates = _find_brand_qualified_path_variant_matches(
        normalized_url,
        article_urls,
    )
    if len(brand_qualified_candidates) == 1:
        return brand_qualified_candidates[0]

    raise DigestCompositionError(
        f"Digest payload field '{field_name}' references unknown article URL: {url}"
    )


def _sanitize_digest_url(url: str) -> str:
    return url.strip().rstrip(".,);]")


def _canonicalize_digest_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def _find_canonical_url_matches(url: str, article_urls: set[str]) -> list[str]:
    canonical_target = _canonicalize_digest_url(url)
    return sorted(
        candidate
        for candidate in article_urls
        if _canonicalize_digest_url(candidate) == canonical_target
    )


def _find_safe_truncated_url_matches(
    url: str,
    article_urls: set[str],
) -> list[str]:
    target_identity = _split_digest_url_identity(url)
    if target_identity is None:
        return []

    return sorted(
        candidate
        for candidate in article_urls
        if _is_safe_truncated_url_match(
            url,
            candidate,
            target_identity=target_identity,
        )
    )


def _is_safe_truncated_url_match(
    url: str,
    candidate: str,
    *,
    target_identity: tuple[str, str, str],
) -> bool:
    if len(url) >= len(candidate) or not candidate.startswith(url):
        return False

    candidate_identity = _split_digest_url_identity(candidate)
    if candidate_identity != target_identity:
        return False

    suffix = candidate[len(url) :]
    return url.endswith(("?", "#")) or suffix[:1] in {"?", "#"}


def _split_digest_url_identity(url: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return None

    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return scheme, host, path


def _find_brand_qualified_path_variant_matches(
    url: str,
    article_urls: set[str],
) -> list[str]:
    target = _split_digest_url_parts(url)
    if target is None:
        return []

    target_host, target_segments = target
    brand_tokens = _extract_host_brand_tokens(target_host)
    if len(target_segments) < 3 or not brand_tokens:
        return []

    matches: list[str] = []
    for candidate in article_urls:
        candidate_parts = _split_digest_url_parts(candidate)
        if candidate_parts is None:
            continue

        candidate_host, candidate_segments = candidate_parts
        if candidate_host != target_host:
            continue
        if len(candidate_segments) != len(target_segments):
            continue

        differing_indexes = [
            index
            for index, (target_segment, candidate_segment) in enumerate(
                zip(target_segments, candidate_segments, strict=True)
            )
            if target_segment.casefold() != candidate_segment.casefold()
        ]
        if len(differing_indexes) != 1:
            continue

        differing_index = differing_indexes[0]
        if differing_index >= len(target_segments) - 2:
            continue

        if not _is_brand_qualified_segment_variant(
            target_segments[differing_index],
            candidate_segments[differing_index],
            brand_tokens=brand_tokens,
        ):
            continue

        matches.append(candidate)

    return sorted(matches)


def _split_digest_url_parts(url: str) -> tuple[str, list[str]] | None:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return None

    path_segments = [segment for segment in parsed.path.split("/") if segment]
    return host, path_segments


def _extract_host_brand_tokens(host: str) -> set[str]:
    first_label = host.split(".", maxsplit=1)[0]
    return {
        token
        for token in re.split(r"[^a-z0-9]+", first_label.casefold())
        if token
    }


def _is_brand_qualified_segment_variant(
    left: str,
    right: str,
    *,
    brand_tokens: set[str],
) -> bool:
    left_tokens = [token for token in re.split(r"[-_]+", left.casefold()) if token]
    right_tokens = [token for token in re.split(r"[-_]+", right.casefold()) if token]
    if not left_tokens or not right_tokens:
        return False
    if abs(len(left_tokens) - len(right_tokens)) != 1:
        return False

    longer_tokens = left_tokens if len(left_tokens) > len(right_tokens) else right_tokens
    shorter_tokens = right_tokens if longer_tokens is left_tokens else left_tokens

    extra_token: str | None = None
    if shorter_tokens == longer_tokens[1:]:
        extra_token = longer_tokens[0]
    elif shorter_tokens == longer_tokens[:-1]:
        extra_token = longer_tokens[-1]

    return extra_token in brand_tokens if extra_token is not None else False


def _require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")

    normalized = value.strip()
    if not allow_empty and not normalized:
        raise DigestCompositionError(f"Digest payload field '{field_name}' must not be empty")
    return normalized


def resolve_digest_source(
    value: Any,
    *,
    field_name: str,
    article: ScoredArticle,
) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized

    fallback = article.source.strip()
    if fallback:
        return fallback
    raise DigestCompositionError(f"Digest payload field '{field_name}' must be a string")


def _resolve_digest_date(
    value: Any,
    *,
    article: ScoredArticle,
) -> str:
    if isinstance(value, str):
        return value.strip()
    return (article.published_at or "").strip()


__all__ = [
    "DigestCompositionError",
    "DigestItem",
    "parse_digest_item",
    "parse_digest_item_list",
    "resolve_digest_source",
    "resolve_digest_url",
]
