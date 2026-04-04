"""Shared helpers for renderer output formatting."""

from __future__ import annotations

from src.analyzer.digest import Digest


def count_unique_sources(digest: Digest) -> int:
    """Count unique, non-empty source names represented in a digest."""
    normalized_sources: set[str] = set()

    if digest.top_story.source.strip():
        normalized_sources.add(digest.top_story.source.strip().casefold())

    for item in digest.key_developments:
        if item.source.strip():
            normalized_sources.add(item.source.strip().casefold())

    for item in digest.on_our_radar:
        if item.source.strip():
            normalized_sources.add(item.source.strip().casefold())

    for hit in digest.quick_hits:
        if hit.source.strip():
            normalized_sources.add(hit.source.strip().casefold())

    return len(normalized_sources)


def format_source_count_label(source_count: int) -> str:
    """Return a human-readable source count label for the header."""
    suffix = "source" if source_count == 1 else "sources"
    return f"{source_count} {suffix}"
