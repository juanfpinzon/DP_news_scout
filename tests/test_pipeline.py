from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pytest
from dotenv import dotenv_values
import src.main as main_module

from src.analyzer.digest import Digest, DigestItem, QuickHit
from src.analyzer.relevance import ScoredArticle
from src.fetcher import FetchSummary, load_source_registry as load_real_source_registry
from src.fetcher.models import RawArticle, Source
from src.main import run_pipeline
from src.renderer.html_email import render_digest as render_html_digest
from src.renderer.plaintext import render_plaintext as render_digest_plaintext
from src.storage.db import ArticleRecord, get_recent_urls, save_articles
from src.utils.config import (
    DEFAULT_ENV_FILE,
    AppConfig,
    EnvConfig,
    RecipientConfig,
    Settings,
)


REAL_PIPELINE_SOURCE_NAMES = {
    "Spend Matters",
    "CPO Rising",
    "Hackett Group Procurement",
}


def test_run_pipeline_happy_path_sends_digest_and_updates_run(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)
    sent: dict[str, object] = {}

    monkeypatch.setattr(
        "src.main.load_source_registry",
        lambda: [_make_source("Source A"), _make_source("Source B")],
    )

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[
                _make_raw_article(1, source="Source A"),
                _make_raw_article(2, source="Source B"),
            ],
            sources_attempted=2,
            sources_succeeded=2,
            sources_failed=0,
            articles_found=2,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [_make_scored_article(1, source="Source A"), _make_scored_article(2, source="Source B")]

    async def fake_compose_digest(*_args, **_kwargs):
        return _make_digest()

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")

    def fake_send_digest(html: str, plaintext: str, subject: str, **kwargs) -> bool:
        sent["html"] = html
        sent["plaintext"] = plaintext
        sent["subject"] = subject
        sent["kwargs"] = kwargs
        return True

    monkeypatch.setattr("src.main.send_digest", fake_send_digest)

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "success"
    assert result.run_id == 1
    assert result.issue_number == 1
    assert result.articles_found == 2
    assert result.relevant_articles == 2
    assert result.articles_included == 4
    assert result.email_sent is True
    assert sent["html"] == "<html>digest</html>"
    assert sent["plaintext"] == "digest"
    assert sent["subject"] == "Digital Procurement News Scout | April 4, 2026 | Issue #1"
    assert sent["kwargs"] == {"config": config, "run_id": 1}

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT id, status, sources_fetched, articles_found, articles_included, error_log "
            "FROM pipeline_runs"
        ).fetchone()

    assert row == (1, "success", 2, 2, 4, None)
    assert get_recent_urls(config.settings.database_path, days=7) == {
        "https://example.com/article-1",
        "https://example.com/article-2",
    }


def test_run_pipeline_uses_issue_number_override_when_configured(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False, issue_number_override=0)
    sent: dict[str, object] = {}

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[_make_raw_article(1, source="Source A")],
            sources_attempted=1,
            sources_succeeded=1,
            sources_failed=0,
            articles_found=1,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [_make_scored_article(1, source="Source A")]

    async def fake_compose_digest(*_args, **_kwargs):
        return _make_digest()

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda html, plaintext, subject, **kwargs: sent.update(
            {"html": html, "plaintext": plaintext, "subject": subject, "kwargs": kwargs}
        )
        or True,
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "success"
    assert result.run_id == 1
    assert result.issue_number == 0
    assert sent["subject"] == "Digital Procurement News Scout | April 4, 2026 | Issue #0"


def test_run_pipeline_passes_ignore_seen_db_to_fetcher(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**kwargs):
        captured.update(kwargs)
        return _make_fetch_summary(
            articles=[_make_raw_article(1, source="Source A")],
            sources_attempted=1,
            sources_succeeded=1,
            sources_failed=0,
            articles_found=1,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [_make_scored_article(1, source="Source A")]

    async def fake_compose_digest(*_args, **_kwargs):
        return _make_digest()

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr("src.main.send_digest", lambda *_args, **_kwargs: True)

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
        ignore_seen_db=True,
    )

    assert result.status == "success"
    assert captured["use_database_seen_urls"] is False


def test_run_pipeline_can_reuse_articles_from_database(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)
    captured: dict[str, object] = {}

    save_articles(
        config.settings.database_path,
        [
            ArticleRecord(
                url="https://example.com/article-1",
                title="Article 1",
                source="Source A",
                published_at="2026-04-04T08:00:00+00:00",
                fetched_at="2026-04-04T08:05:00+00:00",
                content_snippet="Summary 1",
            )
        ],
    )

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])
    monkeypatch.setattr(
        "src.main.fetch_all_sources_report",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("fetch_all_sources_report should not be called")),
    )

    async def fake_score_articles(raw_articles, **_kwargs):
        captured["raw_articles"] = raw_articles
        return [_make_scored_article(1, source="Source A")]

    async def fake_compose_digest(*_args, **_kwargs):
        return _make_digest()

    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr("src.main.send_digest", lambda *_args, **_kwargs: True)

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
        reuse_seen_db=True,
    )

    assert result.status == "success"
    assert len(captured["raw_articles"]) == 1
    assert captured["raw_articles"][0].url == "https://example.com/article-1"
    assert captured["raw_articles"][0].summary == "Summary 1"


def test_run_pipeline_dry_run_skips_send(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=True)

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[_make_raw_article(1, source="Source A")],
            sources_attempted=1,
            sources_succeeded=1,
            sources_failed=0,
            articles_found=1,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [_make_scored_article(1, source="Source A")]

    async def fake_compose_digest(*_args, **_kwargs):
        return _make_digest()

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("send_digest should not be called")),
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "success"
    assert result.email_sent is False
    assert result.dry_run is True
    assert get_recent_urls(config.settings.database_path, days=7) == set()


def test_run_pipeline_sends_no_news_notice_when_no_relevant_articles(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)
    sent: dict[str, str] = {}

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[_make_raw_article(1, source="Source A")],
            sources_attempted=1,
            sources_succeeded=1,
            sources_failed=0,
            articles_found=1,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return []

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda html, plaintext, subject, **_kwargs: sent.update(
            {"html": html, "plaintext": plaintext, "subject": subject}
        )
        or True,
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "success"
    assert result.articles_found == 1
    assert result.relevant_articles == 0
    assert result.articles_included == 0
    assert result.email_sent is True
    assert sent["subject"] == (
        "Digital Procurement News Scout | April 4, 2026 | No major updates | Issue #1"
    )
    assert "No relevant digital procurement updates" in sent["html"]
    assert "No relevant digital procurement updates" in sent["plaintext"]
    assert get_recent_urls(config.settings.database_path, days=7) == {
        "https://example.com/article-1",
    }


def test_run_pipeline_marks_failed_when_fetch_stage_raises(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**_kwargs):
        raise RuntimeError("network blew up")

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("send_digest should not be called")),
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert result.email_sent is False
    assert result.error == "fetcher stage failed: network blew up"

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT status, sources_fetched, articles_found, articles_included, error_log "
            "FROM pipeline_runs WHERE id = 1"
        ).fetchone()

    assert row == ("failed", 0, 0, 0, "fetcher stage failed: network blew up")
    assert get_recent_urls(config.settings.database_path, days=7) == set()


def test_run_pipeline_marks_failed_when_source_registry_load_fails(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)

    monkeypatch.setattr(
        "src.main.load_source_registry",
        lambda: (_ for _ in ()).throw(ValueError("invalid sources config")),
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert result.error == "source registry stage failed: invalid sources config"
    assert result.email_sent is False

    with sqlite3.connect(config.settings.database_path) as connection:
        row = connection.execute(
            "SELECT status, sources_fetched, articles_found, articles_included, error_log "
            "FROM pipeline_runs WHERE id = 1"
        ).fetchone()

    assert row == (
        "failed",
        0,
        0,
        0,
        "source registry stage failed: invalid sources config",
    )


def test_run_pipeline_marks_failed_when_all_sources_fail(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A")])

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[],
            sources_attempted=1,
            sources_succeeded=0,
            sources_failed=1,
            articles_found=0,
        )

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("send_digest should not be called")),
    )

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "failed"
    assert result.error == "fetcher stage failed: all configured sources failed to fetch"
    assert result.email_sent is False
    assert get_recent_urls(config.settings.database_path, days=7) == set()


def test_run_pipeline_succeeds_when_some_sources_fail(tmp_path, monkeypatch) -> None:
    config = _build_config(tmp_path=tmp_path, dry_run=False)

    monkeypatch.setattr("src.main.load_source_registry", lambda: [_make_source("Source A"), _make_source("Source B")])

    async def fake_fetch_all_sources_report(**_kwargs):
        return _make_fetch_summary(
            articles=[_make_raw_article(1, source="Source A")],
            sources_attempted=2,
            sources_succeeded=1,
            sources_failed=1,
            articles_found=1,
        )

    async def fake_score_articles(*_args, **_kwargs):
        return [_make_scored_article(1, source="Source A")]

    async def fake_compose_digest(*_args, **_kwargs):
        return Digest(
            top_story=DigestItem(
                url="https://example.com/article-1",
                headline="Top story",
                summary="Top summary",
                why_it_matters="Top importance",
                source="Source A",
                date="Apr 4, 2026",
            ),
            key_developments=[],
            on_our_radar=[],
            quick_hits=[],
        )

    monkeypatch.setattr("src.main.fetch_all_sources_report", fake_fetch_all_sources_report)
    monkeypatch.setattr("src.main.score_articles", fake_score_articles)
    monkeypatch.setattr("src.main.compose_digest", fake_compose_digest)
    monkeypatch.setattr("src.main.render_digest", lambda *_args, **_kwargs: "<html>digest</html>")
    monkeypatch.setattr("src.main.render_plaintext", lambda *_args, **_kwargs: "digest")
    monkeypatch.setattr("src.main.send_digest", lambda *_args, **_kwargs: True)

    result = run_pipeline(
        config=config,
        now=datetime(2026, 4, 4, 8, 0, tzinfo=timezone.utc),
    )

    assert result.status == "success"
    assert result.sources_fetched == 1
    assert result.articles_found == 1
    assert result.relevant_articles == 1
    assert result.articles_included == 1
    assert result.email_sent is True


def test_run_pipeline_real_rss_and_llm_dry_run(tmp_path, monkeypatch) -> None:
    if os.getenv("RUN_REAL_PIPELINE_TESTS") != "1":
        pytest.skip("Set RUN_REAL_PIPELINE_TESTS=1 to enable the real RSS/LLM pipeline test")

    if not _has_openrouter_api_key():
        pytest.skip("OPENROUTER_API_KEY is required in the environment or .env")

    selected_sources = [
        source
        for source in load_real_source_registry()
        if source.name in REAL_PIPELINE_SOURCE_NAMES
    ]
    assert len(selected_sources) == len(REAL_PIPELINE_SOURCE_NAMES)

    captured: dict[str, str] = {}

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DPNS_DATABASE_PATH", str(tmp_path / "dpns.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "dpns.log"))
    monkeypatch.setenv("MAX_ARTICLES_PER_SOURCE", "1")
    monkeypatch.setenv("MAX_DIGEST_ITEMS", "3")
    monkeypatch.setenv("RELEVANCE_THRESHOLD", "1")
    monkeypatch.setenv("FETCH_CONCURRENCY", "3")
    monkeypatch.setenv("RSS_LOOKBACK_HOURS", "2160")
    monkeypatch.setenv("PIPELINE_TIMEOUT", "240")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("RATE_LIMIT_SECONDS", "0")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "integration-test")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "dpns-integration-test")
    monkeypatch.setenv("EMAIL_FROM", "news-scout@example.com")

    monkeypatch.setattr("src.main.load_source_registry", lambda: selected_sources)
    monkeypatch.setattr(
        "src.main.send_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("send_digest should not be called during dry runs")
        ),
    )

    def capture_html(digest: Digest, issue_number: int, date: str, **kwargs) -> str:
        html = render_html_digest(digest, issue_number=issue_number, date=date, **kwargs)
        captured["html"] = html
        captured["mode"] = "digest"
        return html

    def capture_plaintext(digest: Digest, issue_number: int, date: str) -> str:
        plaintext = render_digest_plaintext(digest, issue_number=issue_number, date=date)
        captured["plaintext"] = plaintext
        return plaintext

    def capture_no_news_email(*, issue_number: int, date_label: str) -> tuple[str, str]:
        html, plaintext = main_module._build_no_news_email(
            issue_number=issue_number,
            date_label=date_label,
        )
        captured["html"] = html
        captured["plaintext"] = plaintext
        captured["mode"] = "no_news"
        return html, plaintext

    monkeypatch.setattr("src.main.render_digest", capture_html)
    monkeypatch.setattr("src.main.render_plaintext", capture_plaintext)
    monkeypatch.setattr("src.main._build_no_news_email", capture_no_news_email)

    result = run_pipeline(now=datetime.now(timezone.utc))

    assert result.status == "success"
    assert result.dry_run is True
    assert result.email_sent is False
    assert result.run_id == 1
    assert result.issue_number == 1
    assert result.sources_fetched >= 1
    assert result.subject.endswith("Issue #1")

    html = captured["html"]
    plaintext = captured["plaintext"]
    mode = captured["mode"]

    assert "<html" in html.lower()
    assert "</html>" in html.lower()
    assert "{{" not in html
    assert "href=" in html

    assert "DIGITAL PROCUREMENT NEWS SCOUT" in plaintext

    if mode == "digest":
        assert result.articles_found >= 1
        assert result.relevant_articles >= 1
        assert result.articles_included >= 1
        assert "TOP STORY" in html
        assert "TOP STORY" in plaintext
        assert "→ Read more: https://" in plaintext
    else:
        assert mode == "no_news"
        assert result.relevant_articles == 0
        assert result.articles_included == 0
        assert "No relevant digital procurement updates" in html
        assert "No relevant digital procurement updates" in plaintext

    with sqlite3.connect(tmp_path / "dpns.db") as connection:
        row = connection.execute(
            "SELECT status, sources_fetched, articles_found, articles_included, error_log "
            "FROM pipeline_runs WHERE id = 1"
        ).fetchone()

    assert row is not None
    assert row[0] == "success"
    assert row[1] == result.sources_fetched
    assert row[2] == result.articles_found
    assert row[3] == result.articles_included
    assert row[4] is None


def _build_config(*, tmp_path, dry_run: bool, issue_number_override: int | None = None) -> AppConfig:
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
        dry_run=dry_run,
        pipeline_timeout=600,
        fetch_concurrency=5,
        rss_lookback_hours=48,
        dedup_window_days=7,
        request_timeout_seconds=15.0,
        rate_limit_seconds=1.0,
        issue_number_override=issue_number_override,
    )
    env = EnvConfig(
        openrouter_api_key="openrouter-test",
        agentmail_api_key="agentmail-test",
        agentmail_inbox_id="dpnewsscout@agentmail.to",
        email_from="dpnewsscout@agentmail.to",
    )
    recipients = [RecipientConfig(email="juancho704@gmail.com")]
    return AppConfig(
        settings=settings,
        env=env,
        sources=[],
        recipients=recipients,
        recipient_groups={"test": recipients},
        default_recipient_group="test",
    )


def _make_source(name: str) -> Source:
    return Source(
        name=name,
        url=f"https://example.com/{name.lower().replace(' ', '-')}.xml",
        tier=1,
        method="rss",
        active=True,
        category="procurement",
    )


def _make_raw_article(number: int, *, source: str) -> RawArticle:
    return RawArticle(
        url=f"https://example.com/article-{number}",
        title=f"Article {number}",
        source=source,
        source_url="https://example.com/feed.xml",
        category="procurement",
        published_at="2026-04-04T08:00:00+00:00",
        summary=f"Summary {number}",
    )


def _make_scored_article(number: int, *, source: str) -> ScoredArticle:
    return ScoredArticle(
        url=f"https://example.com/article-{number}",
        title=f"Article {number}",
        source=source,
        source_url="https://example.com/feed.xml",
        category="procurement",
        published_at="2026-04-04T08:00:00+00:00",
        summary=f"Summary {number}",
        relevance_score=8,
        reasoning="Relevant to digital procurement",
    )


def _make_digest() -> Digest:
    return Digest(
        top_story=DigestItem(
            url="https://example.com/article-1",
            headline="Top story",
            summary="Top summary",
            why_it_matters="Top importance",
            source="Source A",
            date="Apr 4, 2026",
        ),
        key_developments=[
            DigestItem(
                url="https://example.com/article-2",
                headline="Key development",
                summary="Key summary",
                why_it_matters="Key importance",
                source="Source B",
                date="Apr 4, 2026",
            )
        ],
        on_our_radar=[
            DigestItem(
                url="https://example.com/article-3",
                headline="Radar item",
                summary="Radar summary",
                why_it_matters="Radar importance",
                source="Source C",
                date="Apr 4, 2026",
            )
        ],
        quick_hits=[
            QuickHit(
                url="https://example.com/article-4",
                one_liner="Quick hit one",
                source="Source D",
            )
        ],
    )


def _make_fetch_summary(
    *,
    articles: list[RawArticle],
    sources_attempted: int,
    sources_succeeded: int,
    sources_failed: int,
    articles_found: int,
) -> FetchSummary:
    return FetchSummary(
        articles=articles,
        sources_attempted=sources_attempted,
        sources_succeeded=sources_succeeded,
        sources_failed=sources_failed,
        articles_found=articles_found,
        articles_deduplicated=len(articles),
        articles_saved=0,
    )


def _has_openrouter_api_key() -> bool:
    environment_value = os.getenv("OPENROUTER_API_KEY")
    if environment_value is not None:
        return bool(environment_value.strip())

    env_values = dotenv_values(DEFAULT_ENV_FILE)
    value = env_values.get("OPENROUTER_API_KEY")
    return isinstance(value, str) and bool(value.strip())
