from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.fetcher.models import SearchFallbackConfig, Source
from src.utils.config import CONFIG_DIR, ConfigError
from src.utils.source_validation import validate_source_payload

DEFAULT_SOURCE_REGISTRY = CONFIG_DIR / "sources.yaml"


def load_source_registry(
    path: Path | None = None,
    *,
    active_only: bool = True,
    include_fallback_only: bool = False,
) -> list[Source]:
    registry_path = path or DEFAULT_SOURCE_REGISTRY
    raw_config = _read_registry(registry_path)
    raw_sources = raw_config.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("sources.yaml must contain a 'sources' list")

    sources: list[Source] = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"sources[{index}] must be a mapping")

        source = _build_source(item, index)
        if active_only:
            if source.active:
                sources.append(source)
                continue
            if (
                include_fallback_only
                and source.fallback_search.enabled
                and source.fallback_search.include_when_inactive
            ):
                sources.append(source)
            continue
        sources.append(source)

    return sorted(sources, key=lambda source: (source.tier, source.name.casefold()))


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config file must contain a mapping: {path}")
    return loaded


def _build_source(payload: dict[str, Any], index: int) -> Source:
    normalized = validate_source_payload(payload, index=index, error_cls=ConfigError)

    return Source(
        name=normalized["name"],
        url=normalized["url"],
        tier=normalized["tier"],
        method=normalized["method"],
        active=normalized["active"],
        category=normalized["category"],
        selectors=normalized["selectors"],
        fallback_search=SearchFallbackConfig(**normalized["fallback_search"]),
    )
