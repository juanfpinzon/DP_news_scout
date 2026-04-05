from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-agentmail")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox-123")
    monkeypatch.setenv("EMAIL_FROM", "news-scout@example.com")


def test_load_config_reads_defaults() -> None:
    config = load_config()

    assert config.settings.max_articles_per_source == 10
    assert config.settings.fetch_concurrency == 5
    assert config.settings.rss_lookback_hours == 48
    assert config.settings.dedup_window_days == 7
    assert config.settings.max_digest_items_per_source == 3
    assert config.settings.email_max_width_px == 880
    assert config.settings.issue_number_override == 0
    assert config.settings.timezone == "Central European Time"
    assert config.settings.llm_scoring_model == "anthropic/claude-haiku-4.5"
    assert config.settings.llm_digest_model == "anthropic/claude-sonnet-4-6"
    assert config.settings.llm_model_fallback == "anthropic/claude-haiku-4.5"
    assert len(config.sources) >= 20
    assert config.env.email_from == "news-scout@example.com"
    assert config.default_recipient_group == "test"
    assert set(config.recipient_groups) == {"leadership", "extended", "test"}
    assert [recipient.email for recipient in config.recipients] == ["juancho704@gmail.com"]


def test_load_config_allows_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_DIGEST_ITEMS", "12")

    config = load_config()

    assert config.settings.max_digest_items == 12


def test_load_config_allows_stage_specific_model_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_SCORING_MODEL", "anthropic/claude-haiku-4.5")
    monkeypatch.setenv("LLM_DIGEST_MODEL", "anthropic/claude-sonnet-4-6")

    config = load_config()

    assert config.settings.llm_scoring_model == "anthropic/claude-haiku-4.5"
    assert config.settings.llm_digest_model == "anthropic/claude-sonnet-4-6"


def test_load_config_requires_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    missing_env_file = tmp_path / "missing.env"

    with pytest.raises(ConfigError):
        load_config(env_file=missing_env_file)


def test_load_config_can_use_custom_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENROUTER_API_KEY=from-file",
                "AGENTMAIL_API_KEY=from-file",
                "AGENTMAIL_INBOX_ID=from-file",
                "EMAIL_FROM=from-file@example.com",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(env_file=env_file)

    assert config.env.openrouter_api_key == "from-file"


def test_load_config_rejects_invalid_send_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_SEND_TIME", "9am")

    with pytest.raises(ConfigError):
        load_config()


def test_load_config_rejects_invalid_source_category(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read_yaml(path: Path) -> dict:
        if path.name == "settings.yaml":
            return {
                "max_articles_per_source": 10,
                "max_digest_items": 15,
                "relevance_threshold": 6,
                "digest_send_time": "09:00",
                "timezone": "Central European Time",
                "llm_model": "anthropic/claude-sonnet-4-6",
                "llm_model_fallback": "anthropic/claude-haiku-4.5",
                "database_path": "data/dpns.db",
                "log_level": "INFO",
                "log_file": "data/logs/dpns.jsonl",
                "dry_run": True,
                "pipeline_timeout": 600,
                "fetch_concurrency": 5,
                "rss_lookback_hours": 48,
                "dedup_window_days": 7,
                "request_timeout_seconds": 15.0,
                "rate_limit_seconds": 1.0,
            }
        if path.name == "sources.yaml":
            return {
                "sources": [
                    {
                        "name": "Bad Category Source",
                        "url": "https://example.com/feed.xml",
                        "tier": 1,
                        "method": "rss",
                        "active": True,
                        "category": "unknown",
                    }
                ]
            }
        if path.name == "recipients.yaml":
            return {"recipients": [{"email": "reader@example.com"}]}
        raise AssertionError(path)

    monkeypatch.setattr("src.utils.config._read_yaml", fake_read_yaml)

    with pytest.raises(ConfigError, match="category must be one of"):
        load_config()


def test_load_config_supports_legacy_recipient_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read_yaml(path: Path) -> dict:
        if path.name == "settings.yaml":
            return {
                "max_articles_per_source": 10,
                "max_digest_items": 15,
                "relevance_threshold": 6,
                "digest_send_time": "09:00",
                "timezone": "Central European Time",
                "llm_model": "anthropic/claude-sonnet-4-6",
                "llm_model_fallback": "anthropic/claude-haiku-4.5",
                "database_path": "data/dpns.db",
                "log_level": "INFO",
                "log_file": "data/logs/dpns.jsonl",
                "dry_run": True,
                "pipeline_timeout": 600,
                "fetch_concurrency": 5,
                "rss_lookback_hours": 48,
                "dedup_window_days": 7,
                "request_timeout_seconds": 15.0,
                "rate_limit_seconds": 1.0,
            }
        if path.name == "sources.yaml":
            return {"sources": []}
        if path.name == "recipients.yaml":
            return {
                "default_group": "leadership",
                "recipients": [{"email": "leader@example.com"}],
            }
        raise AssertionError(path)

    monkeypatch.setattr("src.utils.config._read_yaml", fake_read_yaml)

    config = load_config()

    assert config.default_recipient_group == "leadership"
    assert list(config.recipient_groups) == ["leadership"]
    assert [recipient.email for recipient in config.recipients] == ["leader@example.com"]


def test_load_config_uses_legacy_llm_model_for_both_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_yaml(path: Path) -> dict:
        if path.name == "settings.yaml":
            return {
                "max_articles_per_source": 10,
                "max_digest_items": 15,
                "relevance_threshold": 6,
                "digest_send_time": "09:00",
                "timezone": "Central European Time",
                "llm_model": "anthropic/claude-sonnet-4-6",
                "llm_model_fallback": "anthropic/claude-haiku-4.5",
                "database_path": "data/dpns.db",
                "log_level": "INFO",
                "log_file": "data/logs/dpns.jsonl",
                "dry_run": True,
                "pipeline_timeout": 600,
                "fetch_concurrency": 5,
                "rss_lookback_hours": 48,
                "dedup_window_days": 7,
                "request_timeout_seconds": 15.0,
                "rate_limit_seconds": 1.0,
            }
        if path.name == "sources.yaml":
            return {"sources": []}
        if path.name == "recipients.yaml":
            return {"groups": {"test": [{"email": "reader@example.com"}]}, "default_group": "test"}
        raise AssertionError(path)

    monkeypatch.setattr("src.utils.config._read_yaml", fake_read_yaml)

    config = load_config()

    assert config.settings.llm_scoring_model == "anthropic/claude-sonnet-4-6"
    assert config.settings.llm_digest_model == "anthropic/claude-sonnet-4-6"


def test_load_config_prefers_stage_specific_models_over_legacy_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_yaml(path: Path) -> dict:
        if path.name == "settings.yaml":
            return {
                "max_articles_per_source": 10,
                "max_digest_items": 15,
                "relevance_threshold": 6,
                "digest_send_time": "09:00",
                "timezone": "Central European Time",
                "llm_model": "anthropic/claude-sonnet-4-6",
                "llm_scoring_model": "anthropic/claude-haiku-4.5",
                "llm_digest_model": "anthropic/claude-sonnet-4-6",
                "llm_model_fallback": "anthropic/claude-haiku-4.5",
                "database_path": "data/dpns.db",
                "log_level": "INFO",
                "log_file": "data/logs/dpns.jsonl",
                "dry_run": True,
                "pipeline_timeout": 600,
                "fetch_concurrency": 5,
                "rss_lookback_hours": 48,
                "dedup_window_days": 7,
                "request_timeout_seconds": 15.0,
                "rate_limit_seconds": 1.0,
            }
        if path.name == "sources.yaml":
            return {"sources": []}
        if path.name == "recipients.yaml":
            return {"groups": {"test": [{"email": "reader@example.com"}]}, "default_group": "test"}
        raise AssertionError(path)

    monkeypatch.setattr("src.utils.config._read_yaml", fake_read_yaml)
    monkeypatch.setenv("LLM_MODEL", "legacy-env-model")

    config = load_config()

    assert config.settings.llm_scoring_model == "anthropic/claude-haiku-4.5"
    assert config.settings.llm_digest_model == "anthropic/claude-sonnet-4-6"
