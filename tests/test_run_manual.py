from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

from src.fetcher import FetchSummary
from src.main import PipelineResult
from src.utils.config import AppConfig, EnvConfig, RecipientConfig, Settings


@pytest.fixture
def run_manual_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_manual.py"
    spec = importlib.util.spec_from_file_location("run_manual_script_module", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load scripts/run_manual.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_dry_run_overrides_pipeline_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(run_manual_module, "load_config", lambda: config)
    monkeypatch.setattr(run_manual_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        run_manual_module,
        "_current_time",
        lambda: datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    def fake_run_pipeline(*, config, now, ignore_seen_db, reuse_seen_db):
        captured["config"] = config
        captured["now"] = now
        captured["ignore_seen_db"] = ignore_seen_db
        captured["reuse_seen_db"] = reuse_seen_db
        return PipelineResult(
            run_id=1,
            issue_number=1,
            status="success",
            started_at="2026-04-04T08:00:00+00:00",
            completed_at="2026-04-04T08:01:00+00:00",
            sources_fetched=20,
            articles_found=12,
            relevant_articles=5,
            articles_included=4,
            email_sent=False,
            subject="Subject",
            dry_run=config.settings.dry_run,
        )

    monkeypatch.setattr(run_manual_module, "run_pipeline", fake_run_pipeline)

    exit_code = run_manual_module.main(["--dry-run"])

    assert exit_code == 0
    assert captured["config"].settings.dry_run is True

    captured_output = capsys.readouterr()
    assert "Dry-run completed" in captured_output.out


def test_main_passes_testing_fetch_flags_to_run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(run_manual_module, "load_config", lambda: config)
    monkeypatch.setattr(run_manual_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        run_manual_module,
        "_current_time",
        lambda: datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    def fake_run_pipeline(*, config, now, ignore_seen_db, reuse_seen_db):
        captured["ignore_seen_db"] = ignore_seen_db
        captured["reuse_seen_db"] = reuse_seen_db
        return PipelineResult(
            run_id=1,
            issue_number=0,
            status="success",
            started_at="2026-04-04T08:00:00+00:00",
            completed_at="2026-04-04T08:01:00+00:00",
            sources_fetched=1,
            articles_found=1,
            relevant_articles=1,
            articles_included=1,
            email_sent=True,
            subject="Subject",
            dry_run=False,
        )

    monkeypatch.setattr(run_manual_module, "run_pipeline", fake_run_pipeline)

    exit_code = run_manual_module.main(["--ignore-seen-db"])

    assert exit_code == 0
    assert captured == {"ignore_seen_db": True, "reuse_seen_db": False}


def test_main_sources_only_reports_fetch_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)

    monkeypatch.setattr(run_manual_module, "load_config", lambda: config)
    monkeypatch.setattr(run_manual_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        run_manual_module,
        "_current_time",
        lambda: datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        run_manual_module,
        "load_source_registry",
        lambda: [object(), object(), object()],
    )
    captured: dict[str, object] = {}

    async def fake_fetch_all_sources_report(**kwargs):
        captured["persist_to_db"] = kwargs["persist_to_db"]
        return FetchSummary(
            articles=[object(), object()],
            sources_attempted=3,
            sources_succeeded=3,
            sources_failed=0,
            articles_found=2,
            articles_deduplicated=2,
            articles_saved=0,
        )

    monkeypatch.setattr(run_manual_module, "fetch_all_sources_report", fake_fetch_all_sources_report)

    exit_code = run_manual_module.main(["--sources-only"])

    assert exit_code == 0
    assert captured["persist_to_db"] is False
    captured_output = capsys.readouterr()
    assert "Fetched 2 deduplicated articles from 3 configured sources." in captured_output.out


def test_main_preview_saves_files_and_opens_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)
    preview_path = tmp_path / "preview.html"
    browser_calls: list[str] = []

    monkeypatch.setattr(run_manual_module, "load_config", lambda: config)
    monkeypatch.setattr(run_manual_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        run_manual_module,
        "_current_time",
        lambda: datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        run_manual_module,
        "_render_live_digest",
        _async_return(
            run_manual_module.RenderedDigestResult(
                issue_number=1,
                date_label="April 4, 2026",
                subject="Subject",
                html="<html>preview</html>",
                plaintext="preview text",
                raw_article_count=10,
                relevant_article_count=4,
                included_article_count=4,
                no_news=False,
            )
        ),
    )
    monkeypatch.setattr(run_manual_module.webbrowser, "open", lambda url: browser_calls.append(url) or True)

    exit_code = run_manual_module.main(["--preview", "--preview-path", str(preview_path)])

    assert exit_code == 0
    assert preview_path.read_text(encoding="utf-8") == "<html>preview</html>"
    assert preview_path.with_suffix(".txt").read_text(encoding="utf-8") == "preview text"
    assert browser_calls == [preview_path.resolve().as_uri()]

    captured_output = capsys.readouterr()
    assert str(preview_path) in captured_output.out
    assert "Rendered digest for issue #1" in captured_output.out


def test_main_test_email_sends_only_to_requested_recipient(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)
    sent: dict[str, object] = {}

    monkeypatch.setattr(run_manual_module, "load_config", lambda: config)
    monkeypatch.setattr(run_manual_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        run_manual_module,
        "_current_time",
        lambda: datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        run_manual_module,
        "_render_live_digest",
        _async_return(
            run_manual_module.RenderedDigestResult(
                issue_number=1,
                date_label="April 4, 2026",
                subject="Subject",
                html="<html>digest</html>",
                plaintext="digest text",
                raw_article_count=10,
                relevant_article_count=4,
                included_article_count=4,
                no_news=False,
            )
        ),
    )

    def fake_send_digest(html: str, plaintext: str, subject: str, *, config):
        sent["html"] = html
        sent["plaintext"] = plaintext
        sent["subject"] = subject
        sent["config"] = config
        return True

    monkeypatch.setattr(run_manual_module, "send_digest", fake_send_digest)

    exit_code = run_manual_module.main(["--test-email", "reviewer@example.com"])

    assert exit_code == 0
    assert sent["html"] == "<html>digest</html>"
    assert sent["plaintext"] == "digest text"
    assert sent["subject"] == "Subject"
    assert sent["config"].default_recipient_group == run_manual_module.MANUAL_TEST_GROUP
    assert [recipient.email for recipient in sent["config"].recipients] == ["reviewer@example.com"]
    assert list(sent["config"].recipient_groups) == [run_manual_module.MANUAL_TEST_GROUP]

    captured_output = capsys.readouterr()
    assert "Sent test digest to reviewer@example.com" in captured_output.out


def test_render_live_digest_uses_non_persisting_fetch_path_and_fails_on_total_outage(
    monkeypatch: pytest.MonkeyPatch,
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(run_manual_module, "load_source_registry", lambda: [object()])

    async def fake_fetch_all_sources_report(**kwargs):
        captured["persist_to_db"] = kwargs["persist_to_db"]
        return FetchSummary(
            articles=[],
            sources_attempted=1,
            sources_succeeded=0,
            sources_failed=1,
            articles_found=0,
            articles_deduplicated=0,
            articles_saved=0,
        )

    monkeypatch.setattr(run_manual_module, "fetch_all_sources_report", fake_fetch_all_sources_report)

    with pytest.raises(RuntimeError, match="all configured sources failed to fetch"):
        run_manual_module.asyncio.run(
            run_manual_module._render_live_digest(
                config=config,
                now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
            )
        )

    assert captured["persist_to_db"] is False


def test_render_live_digest_uses_issue_number_override(
    monkeypatch: pytest.MonkeyPatch,
    run_manual_module,
) -> None:
    config = _build_config(dry_run=False, issue_number_override=0)

    monkeypatch.setattr(run_manual_module, "load_source_registry", lambda: [object()])
    monkeypatch.setattr(run_manual_module, "_next_issue_number", lambda _db_path: 7)

    async def fake_fetch_all_sources_report(**_kwargs):
        return FetchSummary(
            articles=[object()],
            sources_attempted=1,
            sources_succeeded=1,
            sources_failed=0,
            articles_found=1,
            articles_deduplicated=1,
            articles_saved=0,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [object()]

    async def fake_compose_digest(*_args, **_kwargs):
        class _Digest:
            top_story = object()
            key_developments = []
            on_our_radar = []
            quick_hits = []

        return _Digest()

    monkeypatch.setattr(run_manual_module, "fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr(run_manual_module, "score_articles", fake_score_articles)
    monkeypatch.setattr(run_manual_module, "compose_digest", fake_compose_digest)
    monkeypatch.setattr(run_manual_module, "render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr(run_manual_module, "render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr(run_manual_module, "_count_digest_articles", lambda _digest: 1)

    rendered = run_manual_module.asyncio.run(
        run_manual_module._render_live_digest(
            config=config,
            now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
        )
    )

    assert rendered.issue_number == 0
    assert rendered.subject == "Digital Procurement News Scout | April 4, 2026 | Issue #0"


def _build_config(*, dry_run: bool, issue_number_override: int | None = None) -> AppConfig:
    settings = Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Central European Time",
        llm_scoring_model="anthropic/claude-haiku-4.5",
        llm_digest_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-haiku-4.5",
        database_path="/tmp/dpns-test.db",
        log_level="INFO",
        log_file="/tmp/dpns-test.log",
        dry_run=dry_run,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
        issue_number_override=issue_number_override,
    )
    recipients = [RecipientConfig(email="juancho704@gmail.com")]
    return AppConfig(
        settings=settings,
        env=EnvConfig(
            openrouter_api_key="openrouter-test",
            agentmail_api_key="agentmail-test",
            agentmail_inbox_id="dpnewsscout@agentmail.to",
            email_from="dpnewsscout@agentmail.to",
        ),
        sources=[],
        recipients=recipients,
        recipient_groups={"test": recipients},
        default_recipient_group="test",
    )


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner
