from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest

from src.fetcher.models import Source


@pytest.fixture
def seed_sources_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "seed_sources.py"
    spec = importlib.util.spec_from_file_location("seed_sources_test_module", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load scripts/seed_sources.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validate_all_can_include_inactive(monkeypatch: pytest.MonkeyPatch, seed_sources_module) -> None:
    captured: list[bool] = []

    def fake_load_source_registry(*, active_only: bool = True):
        captured.append(active_only)
        return []

    monkeypatch.setattr(seed_sources_module, "load_source_registry", fake_load_source_registry)

    asyncio.run(
        seed_sources_module.validate_all(timeout=1.0, verbose=False, include_inactive=False)
    )
    asyncio.run(
        seed_sources_module.validate_all(timeout=1.0, verbose=False, include_inactive=True)
    )

    assert captured == [True, False]


def test_summarize_results_counts_warnings_and_failures(seed_sources_module) -> None:
    source = Source(
        name="Example",
        url="https://example.com/feed.xml",
        tier=1,
        method="rss",
        active=True,
        category="trade_media",
    )
    ok_result = seed_sources_module.SourceCheckResult(source=source, reachable=True)
    warn_result = seed_sources_module.SourceCheckResult(
        source=source,
        reachable=True,
        warnings=["selector warning"],
    )
    fail_result = seed_sources_module.SourceCheckResult(
        source=source,
        reachable=False,
        error="HTTP 500",
    )

    counts = seed_sources_module.summarize_results([ok_result, warn_result, fail_result])

    assert counts.ok == 1
    assert counts.warnings == 1
    assert counts.failed == 1
