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
    assert config.settings.timezone == "Central European Time"
    assert config.settings.llm_model == "anthropic/claude-sonnet-4-6"
    assert config.settings.llm_model_fallback == "anthropic/claude-4-5-haiku"
    assert len(config.sources) >= 20
    assert config.env.email_from == "news-scout@example.com"


def test_load_config_allows_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_DIGEST_ITEMS", "12")

    config = load_config()

    assert config.settings.max_digest_items == 12


def test_load_config_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")

    with pytest.raises(ConfigError):
        load_config()


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
