"""Plain-text fallback renderer for the daily digest email."""

from __future__ import annotations

import os
from textwrap import fill

from src.analyzer.digest import Digest, DigestItem
from src.renderer.common import count_unique_sources, format_source_count_label

LINE_WIDTH = 60
SEPARATOR = "=" * LINE_WIDTH
THIN_SEPARATOR = "\u2500" * LINE_WIDTH
DEFAULT_FEEDBACK_EMAIL = "news-scout@example.com"


def render_plaintext(digest: Digest, issue_number: int, date: str) -> str:
    """Render a Digest into a structured plain-text string.

    Args:
        digest: The composed digest dataclass.
        issue_number: Sequential issue number for the header.
        date: Human-readable date string (e.g. "April 4, 2026").

    Returns:
        Plain-text string suitable for the text/plain MIME part.
    """
    parts: list[str] = []
    source_count_label = format_source_count_label(count_unique_sources(digest))

    # Header
    parts.append(SEPARATOR)
    parts.append(_center("DIGITAL PROCUREMENT NEWS SCOUT"))
    parts.append(_center(f"{date} · Issue #{issue_number} · {source_count_label}"))
    parts.append(SEPARATOR)

    # Top Story
    parts.append("")
    parts.append("★ TOP STORY")
    parts.append("")
    parts.append(_render_item(digest.top_story))

    # Key Developments
    if digest.key_developments:
        parts.append("")
        parts.append("KEY DEVELOPMENTS")
        parts.append(THIN_SEPARATOR)
        for i, item in enumerate(digest.key_developments, 1):
            parts.append("")
            parts.append(
                _wrap(
                    item.headline,
                    width=LINE_WIDTH,
                    initial_indent=f"{i}. ",
                    subsequent_indent="   ",
                )
            )
            parts.append(_indent(_wrap(item.summary, width=LINE_WIDTH - 3)))
            parts.append(
                _indent(
                    _wrap(
                        item.why_it_matters,
                        width=LINE_WIDTH - 3,
                        initial_indent="Why it matters: ",
                        subsequent_indent=" " * len("Why it matters: "),
                    )
                )
            )
            parts.append(_indent(f"→ {item.url}"))
            source_line = item.source
            if item.date:
                source_line += f" · {item.date}"
            parts.append(_indent(source_line))

    # On Our Radar
    if digest.on_our_radar:
        parts.append("")
        parts.append("ON OUR RADAR")
        parts.append(THIN_SEPARATOR)
        for item in digest.on_our_radar:
            parts.append("")
            parts.append(_wrap(item.headline, width=LINE_WIDTH))
            parts.append(_wrap(item.summary, width=LINE_WIDTH))
            parts.append(f"→ {item.url}")
            source_line = item.source
            if item.date:
                source_line += f" · {item.date}"
            parts.append(source_line)

    # Global Macro Briefing
    if digest.global_briefing:
        parts.append("")
        parts.append("GLOBAL MACRO BRIEFING")
        parts.append(THIN_SEPARATOR)
        for item in digest.global_briefing:
            parts.append("")
            parts.append(_wrap(item.headline, width=LINE_WIDTH))
            parts.append(_wrap(item.summary, width=LINE_WIDTH))
            parts.append(
                _wrap(
                    item.why_it_matters,
                    width=LINE_WIDTH,
                    initial_indent="Why it matters: ",
                    subsequent_indent=" " * len("Why it matters: "),
                )
            )
            parts.append(f"→ {item.url}")
            source_line = item.source
            if item.date:
                source_line += f" · {item.date}"
            parts.append(source_line)

    # Quick Hits
    if digest.quick_hits:
        parts.append("")
        parts.append("QUICK HITS")
        parts.append(THIN_SEPARATOR)
        for hit in digest.quick_hits:
            parts.append(
                _wrap(
                    f"{hit.one_liner} — {hit.source}",
                    width=LINE_WIDTH,
                    initial_indent="• ",
                    subsequent_indent="  ",
                )
            )
            parts.append(f"  → {hit.url}")

    # Footer
    parts.append("")
    parts.append(THIN_SEPARATOR)
    parts.append(_center("Curated by Digital Procurement News Scout"))
    parts.append(_center("for PepsiCo Digital Procurement"))
    feedback = _build_feedback_text()
    if feedback:
        parts.append(_center(f"Feedback: {feedback}"))
    parts.append(_center(f"{date} · Issue #{issue_number}"))
    parts.append(_center("Vibe-coded with ❤️ by Juan Pinzon"))
    parts.append("")

    return "\n".join(parts)


def _render_item(item: DigestItem) -> str:
    """Render a full DigestItem block (used for the Top Story)."""
    lines: list[str] = []
    lines.append(_wrap(item.headline, width=LINE_WIDTH))
    lines.append(_wrap(item.summary, width=LINE_WIDTH))
    lines.append("")
    lines.append(
        _wrap(
            item.why_it_matters,
            width=LINE_WIDTH,
            initial_indent="Why it matters: ",
            subsequent_indent=" " * len("Why it matters: "),
        )
    )
    lines.append(f"→ Read more: {item.url}")
    source_line = item.source
    if item.date:
        source_line += f" · {item.date}"
    lines.append(source_line)
    return "\n".join(lines)


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _center(text: str) -> str:
    return text.center(LINE_WIDTH)


def _wrap(
    text: str,
    *,
    width: int,
    initial_indent: str = "",
    subsequent_indent: str = "",
) -> str:
    normalized = " ".join(text.split())
    return fill(
        normalized,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _build_feedback_text() -> str:
    feedback_url = os.getenv("FEEDBACK_URL", "").strip()
    if feedback_url:
        return feedback_url

    feedback_email = (
        os.getenv("FEEDBACK_EMAIL", "").strip()
        or os.getenv("EMAIL_FROM", "").strip()
        or DEFAULT_FEEDBACK_EMAIL
    )
    return feedback_email
