from __future__ import annotations

import asyncio
import sqlite3
import re

import pytest

from src import main as main_module
from src.main import PipelineResult
from src.fetcher.models import Source
from src.utils.config import AppConfig, EnvConfig, RecipientConfig, Settings
from src.utils.progress import build_stdout_progress_callback


def test_build_stdout_progress_callback_emits_timestamped_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    callback = build_stdout_progress_callback()

    callback("Progress message")

    captured = capsys.readouterr()
    assert re.search(r"^\[\d{2}:\d{2}:\d{2}\] Progress message\n$", captured.out)


def test_main_uses_stdout_progress_in_github_actions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _build_config(dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "configure_logging", lambda _config: None)

    def fake_run_pipeline(*, config, progress_callback, **_kwargs):
        captured["progress_callback"] = progress_callback
        assert progress_callback is not None
        progress_callback("Loaded 2 configured sources for issue #1.")
        return PipelineResult(
            run_id=1,
            issue_number=1,
            status="success",
            started_at="2026-04-05T08:00:00+00:00",
            completed_at="2026-04-05T08:01:00+00:00",
            sources_fetched=2,
            articles_found=4,
            relevant_articles=3,
            articles_included=3,
            email_sent=False,
            subject="Subject",
            dry_run=config.settings.dry_run,
        )

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    main_module.main()

    assert callable(captured["progress_callback"])
    captured_output = capsys.readouterr()
    assert "Starting pipeline run (dry_run=False)." in captured_output.out
    assert "Using database /tmp/dpns-test.db and log file /tmp/dpns-test.log." in captured_output.out
    assert "Loaded 2 configured sources for issue #1." in captured_output.out


def test_main_keeps_stdout_progress_disabled_outside_github_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_config(dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "configure_logging", lambda _config: None)

    def fake_run_pipeline(*, progress_callback, **_kwargs):
        captured["progress_callback"] = progress_callback
        return PipelineResult(
            run_id=1,
            issue_number=1,
            status="success",
            started_at="2026-04-05T08:00:00+00:00",
            completed_at="2026-04-05T08:01:00+00:00",
            sources_fetched=2,
            articles_found=4,
            relevant_articles=3,
            articles_included=3,
            email_sent=False,
            subject="Subject",
            dry_run=False,
        )

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    main_module.main()

    assert captured["progress_callback"] is None


def test_run_pipeline_marks_timeout_when_pipeline_exceeds_deadline(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_config(dry_run=False)
    config.settings.database_path = str(tmp_path / "dpns.db")
    config.settings.log_file = str(tmp_path / "dpns.log")
    config.settings.pipeline_timeout = 0.01

    monkeypatch.setattr(
        main_module,
        "load_source_registry",
        lambda **_kwargs: [Source(name="Source A", url="https://example.com/a", tier=1, method="rss", active=True, category="procurement")],
    )

    async def never_finishes(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "_run_pipeline_async", never_finishes)
    monkeypatch.setattr(
        main_module,
        "send_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("send_digest should not be called")),
    )

    result = main_module.run_pipeline(config=config)

    assert result.status == "timeout"
    assert result.email_sent is False
    assert result.error == "Pipeline timed out after 0.01 seconds"

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT status, completed_at, error_log FROM pipeline_runs WHERE id = 1"
        ).fetchone()

    assert row is not None
    assert row[0] == "timeout"
    assert row[1] is not None
    assert row[2] == "Pipeline timed out after 0.01 seconds"


def _build_config(*, dry_run: bool) -> AppConfig:
    recipients = [RecipientConfig(email="tester@example.com", name="Tester")]
    settings = Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Europe/Madrid",
        llm_scoring_model="anthropic/claude-haiku-4.5",
        llm_digest_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-haiku-4.5",
        database_path="/tmp/dpns-test.db",
        log_level="INFO",
        log_file="/tmp/dpns-test.log",
        dry_run=dry_run,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=168,
        dedup_window_days=14,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
        max_digest_items_per_source=3,
        email_max_width_px=880,
        issue_number_override=0,
        recency_priority_window_days=7,
        reuse_seen_db_window_days=14,
    )
    env = EnvConfig(
        openrouter_api_key="test-openrouter",
        agentmail_api_key="test-agentmail",
        agentmail_inbox_id="test-inbox",
        email_from="news-scout@example.com",
    )
    return AppConfig(
        settings=settings,
        env=env,
        sources=[],
        recipients=recipients,
        recipient_groups={"test": recipients},
        default_recipient_group="test",
    )
