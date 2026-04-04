from __future__ import annotations

import sqlite3

from src.sender.email_sender import send_digest
from src.storage.db import PipelineRunRecord, log_run, utc_now_iso
from src.utils.config import AppConfig, EnvConfig, RecipientConfig, Settings


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send(self, inbox_id: str, **kwargs):
        self.calls.append((inbox_id, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeInboxes:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeAgentMailClient:
    def __init__(self, responses):
        self.inboxes = FakeInboxes(responses)


class FakeResponse:
    id = "msg_123"


def test_send_digest_sends_bcc_and_logs_delivery(tmp_path) -> None:
    config = _build_config(
        tmp_path=tmp_path,
        groups={
            "leadership": [
                RecipientConfig(email="leader@example.com", name="Leader"),
                RecipientConfig(email="Leader@example.com", name="Duplicate"),
                RecipientConfig(email="cpo@example.com", name="CPO"),
            ]
        },
    )
    client = FakeAgentMailClient([FakeResponse()])
    run_id = _create_run(config)

    sent = send_digest(
        "<html>Digest</html>",
        "Digest",
        "DPNS | April 4, 2026",
        config=config,
        run_id=run_id,
        client=client,
        sleep_fn=lambda _: None,
    )

    assert sent is True
    assert client.inboxes.messages.calls == [
        (
            "inbox_123",
            {
                "to": "news-scout@example.com",
                "bcc": ["leader@example.com", "cpo@example.com"],
                "subject": "DPNS | April 4, 2026",
                "html": "<html>Digest</html>",
                "text": "Digest",
                "reply_to": "news-scout@example.com",
            },
        )
    ]

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT run_id, recipient_count, status, error FROM delivery_log"
        ).fetchone()

    assert row == (run_id, 2, "sent", None)


def test_send_digest_retries_before_success(tmp_path) -> None:
    config = _build_config(
        tmp_path=tmp_path,
        groups={"leadership": [RecipientConfig(email="leader@example.com")]},
    )
    client = FakeAgentMailClient(
        [RuntimeError("temporary outage"), RuntimeError("still failing"), FakeResponse()]
    )
    sleep_calls: list[float] = []

    sent = send_digest(
        "<html>Digest</html>",
        "Digest",
        "DPNS | April 4, 2026",
        config=config,
        client=client,
        sleep_fn=sleep_calls.append,
    )

    assert sent is True
    assert len(client.inboxes.messages.calls) == 3
    assert sleep_calls == [1.0, 2.0]


def test_send_digest_returns_false_after_retry_exhaustion(tmp_path) -> None:
    config = _build_config(
        tmp_path=tmp_path,
        groups={"leadership": [RecipientConfig(email="leader@example.com")]},
    )
    client = FakeAgentMailClient(
        [
            RuntimeError("temporary outage"),
            RuntimeError("still failing"),
            RuntimeError("permanent failure"),
        ]
    )
    run_id = _create_run(config)

    sent = send_digest(
        "<html>Digest</html>",
        "Digest",
        "DPNS | April 4, 2026",
        config=config,
        run_id=run_id,
        client=client,
        sleep_fn=lambda _: None,
    )

    assert sent is False

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT run_id, recipient_count, status, error FROM delivery_log"
        ).fetchone()

    assert row == (run_id, 1, "failed", "permanent failure")


def test_send_digest_returns_false_when_group_is_empty(tmp_path) -> None:
    config = _build_config(
        tmp_path=tmp_path,
        groups={"leadership": [], "test": [RecipientConfig(email="tester@example.com")]},
    )
    client = FakeAgentMailClient([FakeResponse()])

    sent = send_digest(
        "<html>Digest</html>",
        "Digest",
        "DPNS | April 4, 2026",
        config=config,
        client=client,
    )

    assert sent is False
    assert client.inboxes.messages.calls == []


def test_send_digest_uses_requested_group(tmp_path) -> None:
    config = _build_config(
        tmp_path=tmp_path,
        groups={
            "leadership": [RecipientConfig(email="leader@example.com")],
            "test": [RecipientConfig(email="tester@example.com")],
        },
    )
    client = FakeAgentMailClient([FakeResponse()])

    sent = send_digest(
        "<html>Digest</html>",
        "Digest",
        "DPNS | April 4, 2026",
        config=config,
        group="test",
        client=client,
        sleep_fn=lambda _: None,
    )

    assert sent is True
    assert client.inboxes.messages.calls[0][1]["bcc"] == ["tester@example.com"]


def _build_config(
    *,
    tmp_path,
    groups: dict[str, list[RecipientConfig]],
    default_group: str = "leadership",
) -> AppConfig:
    settings = Settings(
        max_articles_per_source=10,
        max_digest_items=15,
        relevance_threshold=6,
        digest_send_time="09:00",
        timezone="Central European Time",
        llm_model="anthropic/claude-sonnet-4-6",
        llm_model_fallback="anthropic/claude-4-5-haiku",
        database_path=str(tmp_path / "dpns.db"),
        log_level="INFO",
        log_file=str(tmp_path / "dpns.log"),
        dry_run=False,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
    )
    env = EnvConfig(
        openrouter_api_key="openrouter-test",
        agentmail_api_key="agentmail-test",
        agentmail_inbox_id="inbox_123",
        email_from="news-scout@example.com",
    )
    recipients = list(groups.get(default_group, []))
    return AppConfig(
        settings=settings,
        env=env,
        sources=[],
        recipients=recipients,
        recipient_groups=groups,
        default_recipient_group=default_group,
    )


def _create_run(config: AppConfig) -> int:
    return log_run(
        config.settings.database_path,
        PipelineRunRecord(
            started_at=utc_now_iso(),
            status="started",
        ),
    )
