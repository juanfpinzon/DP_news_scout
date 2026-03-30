from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.fetcher.models import Source
from src.utils.config import CONFIG_DIR, ConfigError

DEFAULT_SOURCE_REGISTRY = CONFIG_DIR / "sources.yaml"


def load_source_registry(
    path: Path | None = None,
    *,
    active_only: bool = True,
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
        if active_only and not source.active:
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
    required_string_fields = ("name", "url", "method", "category")
    for field_name in required_string_fields:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"sources[{index}].{field_name} is required")

    tier = payload.get("tier")
    if not isinstance(tier, int) or tier <= 0:
        raise ConfigError(f"sources[{index}].tier must be greater than 0")

    method = str(payload["method"]).strip()
    if method not in {"rss", "scrape"}:
        raise ConfigError(f"sources[{index}].method must be 'rss' or 'scrape'")

    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise ConfigError(f"sources[{index}].active must be a boolean")

    selectors = payload.get("selectors", {})
    if selectors is None:
        selectors = {}
    if not isinstance(selectors, dict):
        raise ConfigError(f"sources[{index}].selectors must be a mapping when provided")

    return Source(
        name=str(payload["name"]).strip(),
        url=str(payload["url"]).strip(),
        tier=tier,
        method=method,
        active=active,
        category=str(payload["category"]).strip(),
        selectors=dict(selectors),
    )
