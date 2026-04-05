from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ALLOWED_SOURCE_CATEGORIES = {
    "trade_media",
    "analyst",
    "consulting",
    "vendor",
    "mainstream",
    "community",
    "peer_cpg",
}
ALLOWED_SOURCE_METHODS = {"rss", "scrape"}
REQUIRED_SCRAPE_SELECTOR_KEYS = ("article", "title", "link", "date")


def validate_source_payload(
    payload: Mapping[str, Any],
    *,
    index: int,
    error_cls: type[Exception] = ValueError,
) -> dict[str, Any]:
    name = _require_non_empty_string(payload.get("name"), field="name", index=index, error_cls=error_cls)
    url = _require_non_empty_string(payload.get("url"), field="url", index=index, error_cls=error_cls)
    method = _require_non_empty_string(
        payload.get("method"),
        field="method",
        index=index,
        error_cls=error_cls,
    )
    if method not in ALLOWED_SOURCE_METHODS:
        raise error_cls(f"sources[{index}].method must be 'rss' or 'scrape'")

    category = _require_non_empty_string(
        payload.get("category"),
        field="category",
        index=index,
        error_cls=error_cls,
    )
    if category not in ALLOWED_SOURCE_CATEGORIES:
        allowed = ", ".join(sorted(ALLOWED_SOURCE_CATEGORIES))
        raise error_cls(f"sources[{index}].category must be one of: {allowed}")

    tier = payload.get("tier")
    if not isinstance(tier, int) or tier <= 0:
        raise error_cls(f"sources[{index}].tier must be greater than 0")

    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise error_cls(f"sources[{index}].active must be a boolean")

    selectors = payload.get("selectors")
    normalized_selectors = _normalize_selectors(
        selectors,
        index=index,
        method=method,
        error_cls=error_cls,
    )
    fallback_search = _normalize_fallback_search(
        payload.get("fallback_search"),
        index=index,
        error_cls=error_cls,
    )

    return {
        "name": name,
        "url": url,
        "tier": tier,
        "method": method,
        "active": active,
        "category": category,
        "selectors": normalized_selectors,
        "fallback_search": fallback_search,
    }


def _normalize_selectors(
    selectors: Any,
    *,
    index: int,
    method: str,
    error_cls: type[Exception],
) -> dict[str, str]:
    if selectors is None:
        selectors = {}
    if not isinstance(selectors, Mapping):
        raise error_cls(f"sources[{index}].selectors must be a mapping when provided")

    normalized: dict[str, str] = {}
    for key, value in selectors.items():
        if not isinstance(key, str) or not key.strip():
            raise error_cls(f"sources[{index}].selectors keys must be non-empty strings")
        if not isinstance(value, str) or not value.strip():
            raise error_cls(f"sources[{index}].selectors.{key} must be a non-empty string")
        normalized[key.strip()] = value.strip()

    if method == "scrape":
        if not normalized:
            raise error_cls(f"sources[{index}].selectors is required for scrape sources")
        missing_keys = [
            key for key in REQUIRED_SCRAPE_SELECTOR_KEYS if key not in normalized
        ]
        if missing_keys:
            missing = ", ".join(missing_keys)
            raise error_cls(
                f"sources[{index}].selectors is missing required scrape keys: {missing}"
            )

    return normalized


def _normalize_fallback_search(
    fallback_search: Any,
    *,
    index: int,
    error_cls: type[Exception],
) -> dict[str, Any]:
    if fallback_search is None:
        return {
            "configured": False,
            "enabled_explicit": False,
            "enabled": False,
            "include_when_inactive": False,
            "query": None,
            "max_results": None,
        }
    if not isinstance(fallback_search, Mapping):
        raise error_cls(f"sources[{index}].fallback_search must be a mapping when provided")

    enabled_explicit = "enabled" in fallback_search
    enabled = fallback_search.get("enabled", False)
    if not isinstance(enabled, bool):
        raise error_cls(f"sources[{index}].fallback_search.enabled must be a boolean")

    include_when_inactive = fallback_search.get("include_when_inactive", False)
    if not isinstance(include_when_inactive, bool):
        raise error_cls(
            f"sources[{index}].fallback_search.include_when_inactive must be a boolean"
        )

    raw_query = fallback_search.get("query")
    if raw_query is not None and (not isinstance(raw_query, str) or not raw_query.strip()):
        raise error_cls(f"sources[{index}].fallback_search.query must be a non-empty string")
    query = raw_query.strip() if isinstance(raw_query, str) else None

    raw_max_results = fallback_search.get("max_results")
    if raw_max_results is None:
        max_results = None
    else:
        if not isinstance(raw_max_results, int):
            raise error_cls(f"sources[{index}].fallback_search.max_results must be an integer")
        if not 1 <= raw_max_results <= 3:
            raise error_cls(
                f"sources[{index}].fallback_search.max_results must be between 1 and 3"
            )
        max_results = raw_max_results

    return {
        "configured": True,
        "enabled_explicit": enabled_explicit,
        "enabled": enabled,
        "include_when_inactive": include_when_inactive,
        "query": query,
        "max_results": max_results,
    }


def _require_non_empty_string(
    value: Any,
    *,
    field: str,
    index: int,
    error_cls: type[Exception],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_cls(f"sources[{index}].{field} is required")
    return value.strip()
