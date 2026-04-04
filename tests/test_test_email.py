from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def test_email_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "test_email.py"
    spec = importlib.util.spec_from_file_location("test_email_script_module", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load scripts/test_email.py")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_writes_html_and_plaintext_previews(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    test_email_module,
) -> None:
    preview_path = tmp_path / "preview.html"

    exit_code = test_email_module.main(
        [
            "--preview-path",
            str(preview_path),
            "--issue-number",
            "7",
            "--date",
            "April 4, 2026",
        ]
    )

    assert exit_code == 0
    assert preview_path.exists()

    plaintext_path = preview_path.with_suffix(".txt")
    assert plaintext_path.exists()

    html = preview_path.read_text(encoding="utf-8")
    plaintext = plaintext_path.read_text(encoding="utf-8")

    assert "Issue #7" in html
    assert "9 sources" in html
    assert "DIGITAL PROCUREMENT NEWS SCOUT" in html
    assert "TOP STORY" in plaintext
    assert "9 sources" in plaintext
    assert "KEY DEVELOPMENTS" in plaintext

    captured = capsys.readouterr()
    assert str(preview_path) in captured.out
    assert str(plaintext_path) in captured.out


def test_main_sends_test_email_when_recipient_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    test_email_module,
) -> None:
    sent: dict[str, str] = {}

    class FakeMessages:
        def send(self, inbox_id: str, **kwargs) -> None:
            sent["inbox_id"] = inbox_id
            for key, value in kwargs.items():
                sent[key] = value

    class FakeInboxes:
        def __init__(self) -> None:
            self.messages = FakeMessages()

    class FakeAgentMail:
        def __init__(self, *, api_key: str) -> None:
            sent["api_key"] = api_key
            self.inboxes = FakeInboxes()

    monkeypatch.setattr(test_email_module, "AgentMail", FakeAgentMail)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "agentmail-test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox_test_123")
    monkeypatch.setenv("EMAIL_FROM", "news-scout@example.com")

    exit_code = test_email_module.main(
        [
            "--preview-path",
            str(tmp_path / "preview.html"),
            "--date",
            "April 4, 2026",
            "--to",
            "leader@example.com",
        ]
    )

    assert exit_code == 0
    assert sent["api_key"] == "agentmail-test-key"
    assert sent["inbox_id"] == "inbox_test_123"
    assert sent["to"] == "leader@example.com"
    assert sent["reply_to"] == "news-scout@example.com"
    assert sent["subject"] == "Digital Procurement News Scout | April 4, 2026 | Issue #1"
    assert "DIGITAL PROCUREMENT NEWS SCOUT" in sent["html"]
    assert "9 sources" in sent["html"]
    assert "TOP STORY" in sent["text"]
    assert "9 sources" in sent["text"]

    captured = capsys.readouterr()
    assert "Sent test digest to leader@example.com" in captured.out


def test_send_test_email_requires_agentmail_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    test_email_module,
) -> None:
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    monkeypatch.delenv("AGENTMAIL_INBOX_ID", raising=False)

    with pytest.raises(RuntimeError, match="AGENTMAIL_API_KEY"):
        test_email_module.send_test_email(
            recipient="leader@example.com",
            subject="Subject",
            html="<html></html>",
            plaintext="Text",
        )
