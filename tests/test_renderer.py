"""Tests for the HTML email renderer."""

from __future__ import annotations

from src.analyzer.digest import Digest, DigestItem, QuickHit
from src.renderer.html_email import render_digest
from src.renderer.plaintext import render_plaintext


def _make_item(n: int = 1, date: str = "Apr 3, 2026") -> DigestItem:
    return DigestItem(
        url=f"https://example.com/article-{n}",
        headline=f"Headline {n}",
        summary=f"Summary for article {n}.",
        why_it_matters=f"Matters because {n}.",
        source=f"Source {n}",
        date=date,
    )


def _make_quick_hit(n: int = 1) -> QuickHit:
    return QuickHit(
        url=f"https://example.com/qh-{n}",
        one_liner=f"Quick hit {n}",
        source=f"QH Source {n}",
    )


def _make_full_digest() -> Digest:
    return Digest(
        top_story=_make_item(1),
        key_developments=[_make_item(2), _make_item(3)],
        on_our_radar=[_make_item(4)],
        quick_hits=[_make_quick_hit(1), _make_quick_hit(2)],
    )


def _make_minimal_digest() -> Digest:
    return Digest(
        top_story=_make_item(1),
        key_developments=[],
        on_our_radar=[],
        quick_hits=[],
    )


def _make_duplicate_source_digest() -> Digest:
    return Digest(
        top_story=DigestItem(
            url="https://example.com/top",
            headline="Top story",
            summary="Summary.",
            why_it_matters="Important.",
            source="Shared Source",
            date="Apr 4, 2026",
        ),
        key_developments=[
            DigestItem(
                url="https://example.com/key-1",
                headline="Key 1",
                summary="Summary.",
                why_it_matters="Important.",
                source="Shared Source",
                date="Apr 4, 2026",
            ),
            DigestItem(
                url="https://example.com/key-2",
                headline="Key 2",
                summary="Summary.",
                why_it_matters="Important.",
                source="Second Source",
                date="Apr 4, 2026",
            ),
        ],
        on_our_radar=[],
        quick_hits=[
            QuickHit(
                url="https://example.com/hit-1",
                one_liner="Hit 1",
                source="Second Source",
            ),
            QuickHit(
                url="https://example.com/hit-2",
                one_liner="Hit 2",
                source="Third Source",
            ),
        ],
    )


class TestRenderDigest:
    def test_returns_html_string(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=42, date="April 4, 2026")
        assert isinstance(html, str)
        assert html.startswith("<!DOCTYPE html>") or html.startswith("<html")

    def test_accepts_positional_issue_number_and_date(self) -> None:
        html = render_digest(_make_full_digest(), 42, "April 4, 2026")
        assert "Issue #42" in html

    def test_contains_issue_number_and_date(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=99, date="March 15, 2026")
        assert "Issue #99" in html
        assert "March 15, 2026" in html
        assert "6 sources" in html

    def test_contains_all_sections_when_populated(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "TOP STORY" in html
        assert "KEY DEVELOPMENTS" in html
        assert "ON OUR RADAR" in html
        assert "QUICK HITS" in html

    def test_omits_empty_sections(self) -> None:
        html = render_digest(_make_minimal_digest(), issue_number=1, date="April 4, 2026")
        assert "TOP STORY" in html
        assert "KEY DEVELOPMENTS" not in html
        assert "ON OUR RADAR" not in html
        assert "QUICK HITS" not in html

    def test_top_story_content_rendered(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "Headline 1" in html
        assert "Summary for article 1." in html
        assert "Matters because 1." in html
        assert "https://example.com/article-1" in html
        assert "Source 1" in html

    def test_key_developments_rendered(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "Headline 2" in html
        assert "Headline 3" in html

    def test_quick_hits_rendered(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "Quick hit 1" in html
        assert "Quick hit 2" in html
        assert "QH Source 1" in html

    def test_premailer_inlines_css(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        # premailer should inline background-color on the body tag
        assert 'style=' in html
        # The current deep navy header color should appear inline
        assert "#1a2332" in html

    def test_header_counts_unique_sources(self) -> None:
        html = render_digest(_make_duplicate_source_digest(), issue_number=1, date="April 4, 2026")
        assert "3 sources" in html

    def test_primary_article_links_use_dark_headline_color(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        for url in (
            "https://example.com/article-1",
            "https://example.com/article-2",
            "https://example.com/article-4",
        ):
            link_idx = html.index(url)
            link_html = html[max(0, link_idx - 120): link_idx + 180]
            assert "color:#1a2332" in link_html

    def test_mobile_body_copy_has_explicit_15px_rule(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "class=\"mobile-body\"" in html
        assert ".mobile-body" in html
        assert "font-size: 15px;" in html

    def test_header_uses_nested_right_aligned_issue_column(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "Nested two-column table is more reliable in Gmail than an empty spacer cell" in html
        assert 'width="172"' in html
        assert "table-layout:fixed" in html
        assert '<table role="presentation" align="right"' in html

    def test_footer_uses_centered_inner_table(self) -> None:
        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")
        footer_idx = html.index("Questions or suggestions?")
        footer_html = html[max(0, footer_idx - 400): footer_idx + 400]
        assert 'align="center"' in footer_html
        assert "Digital Procurement News Scout" in footer_html

    def test_desktop_container_width_is_configurable(self) -> None:
        html = render_digest(
            _make_full_digest(),
            issue_number=1,
            date="April 4, 2026",
            max_width_px=880,
        )
        assert 'width="880"' in html
        assert "max-width:880px" in html

    def test_footer_renders_feedback_link(self, monkeypatch) -> None:
        monkeypatch.delenv("FEEDBACK_URL", raising=False)
        monkeypatch.setenv("FEEDBACK_EMAIL", "feedback@example.com")

        html = render_digest(_make_full_digest(), issue_number=1, date="April 4, 2026")

        assert "Share feedback" in html
        assert 'href="mailto:feedback@example.com?subject=DPNS%20Feedback"' in html

    def test_empty_date_omits_middot(self) -> None:
        digest = Digest(
            top_story=_make_item(1, date=""),
            key_developments=[],
            on_our_radar=[],
            quick_hits=[],
        )
        html = render_digest(digest, issue_number=1, date="April 4, 2026")
        # The source line for the top story should not have a middot before an empty date
        # Find the source text area — "Source 1" should not be followed by middot
        source_idx = html.index("Source 1")
        after_source = html[source_idx:source_idx + 100]
        assert "&middot;" not in after_source

    def test_html_escapes_special_characters(self) -> None:
        digest = Digest(
            top_story=DigestItem(
                url="https://example.com/xss",
                headline='<script>alert("xss")</script>',
                summary="A & B < C",
                why_it_matters="Important for 'us'",
                source="Source & Co",
                date="Apr 1",
            ),
            key_developments=[],
            on_our_radar=[],
            quick_hits=[],
        )
        html = render_digest(digest, issue_number=1, date="April 4, 2026")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "A &amp; B &lt; C" in html


class TestRenderPlaintext:
    def test_returns_string(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=42, date="April 4, 2026")
        assert isinstance(text, str)

    def test_accepts_positional_issue_number_and_date(self) -> None:
        text = render_plaintext(_make_full_digest(), 42, "April 4, 2026")
        assert "Issue #42" in text

    def test_header_contains_title_date_issue(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=42, date="April 4, 2026")
        assert "DIGITAL PROCUREMENT NEWS SCOUT" in text
        assert "April 4, 2026" in text
        assert "Issue #42" in text
        assert "6 sources" in text

    def test_header_counts_unique_sources(self) -> None:
        text = render_plaintext(_make_duplicate_source_digest(), issue_number=1, date="April 4, 2026")
        assert "3 sources" in text

    def test_top_story_section(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "TOP STORY" in text
        assert "Headline 1" in text
        assert "Summary for article 1." in text
        assert "Matters because 1." in text
        assert "https://example.com/article-1" in text

    def test_key_developments_numbered(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "KEY DEVELOPMENTS" in text
        assert "1. Headline 2" in text
        assert "2. Headline 3" in text

    def test_on_our_radar_section(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "ON OUR RADAR" in text
        assert "Headline 4" in text

    def test_quick_hits_bulleted(self) -> None:
        text = render_plaintext(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "QUICK HITS" in text
        assert "Quick hit 1" in text
        assert "QH Source 1" in text

    def test_omits_empty_sections(self) -> None:
        text = render_plaintext(_make_minimal_digest(), issue_number=1, date="April 4, 2026")
        assert "TOP STORY" in text
        assert "KEY DEVELOPMENTS" not in text
        assert "ON OUR RADAR" not in text
        assert "QUICK HITS" not in text

    def test_empty_date_omits_separator(self) -> None:
        digest = Digest(
            top_story=_make_item(1, date=""),
            key_developments=[],
            on_our_radar=[],
            quick_hits=[],
        )
        text = render_plaintext(digest, issue_number=1, date="April 4, 2026")
        # Source line for top story should not contain the dot separator
        for line in text.splitlines():
            if "Source 1" in line and "Issue" not in line:
                assert "·" not in line
                break

    def test_footer_contains_feedback(self, monkeypatch) -> None:
        monkeypatch.delenv("FEEDBACK_URL", raising=False)
        monkeypatch.setenv("FEEDBACK_EMAIL", "test@example.com")
        text = render_plaintext(_make_full_digest(), issue_number=1, date="April 4, 2026")
        assert "Feedback: test@example.com" in text

    def test_wraps_long_why_it_matters_lines(self) -> None:
        long_reason = (
            "This is a deliberately long why-it-matters explanation that should wrap "
            "cleanly instead of overflowing the plain-text digest layout."
        )
        digest = Digest(
            top_story=DigestItem(
                url="https://example.com/top",
                headline="Top story",
                summary="Top summary.",
                why_it_matters=long_reason,
                source="Source 1",
                date="Apr 4, 2026",
            ),
            key_developments=[],
            on_our_radar=[],
            quick_hits=[],
        )

        text = render_plaintext(digest, issue_number=1, date="April 4, 2026")

        assert "Why it matters: This is a deliberately long why-it-matters" in text
        assert "explanation that should wrap" in text

    def test_wraps_long_numbered_headlines(self) -> None:
        digest = Digest(
            top_story=_make_item(1),
            key_developments=[
                DigestItem(
                    url="https://example.com/article-2",
                    headline=(
                        "A very long procurement headline that should continue on the next "
                        "line with the numbered indentation preserved"
                    ),
                    summary="Summary.",
                    why_it_matters="Important.",
                    source="Source 2",
                    date="Apr 4, 2026",
                )
            ],
            on_our_radar=[],
            quick_hits=[],
        )

        text = render_plaintext(digest, issue_number=1, date="April 4, 2026")

        assert "1. A very long procurement headline" in text
        assert "\n   the next line with the numbered indentation preserved" in text
