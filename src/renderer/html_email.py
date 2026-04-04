"""HTML email renderer — loads the Jinja2 template and inlines CSS via premailer."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from premailer import transform

from src.analyzer.digest import Digest
from src.renderer.common import count_unique_sources, format_source_count_label

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
TEMPLATE_NAME = "digest_email.html"
DEFAULT_FEEDBACK_SUBJECT = "DPNS Feedback"
DEFAULT_FEEDBACK_EMAIL = "news-scout@example.com"
DEFAULT_EMAIL_MAX_WIDTH_PX = 880


def render_digest(
    digest: Digest,
    issue_number: int,
    date: str,
    *,
    max_width_px: int = DEFAULT_EMAIL_MAX_WIDTH_PX,
) -> str:
    """Render a Digest into a fully inlined HTML email string.

    Args:
        digest: The composed digest dataclass.
        issue_number: Sequential issue number for the header.
        date: Human-readable date string (e.g. "April 4, 2026").
        max_width_px: Desktop max width for the email container.

    Returns:
        Complete HTML string with all CSS inlined for email client compatibility.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(TEMPLATE_NAME)

    context = asdict(digest)
    context["issue_number"] = issue_number
    context["date"] = date
    context["source_count_label"] = format_source_count_label(
        count_unique_sources(digest)
    )
    context["feedback_href"] = _build_feedback_href()
    context["max_width_px"] = max_width_px

    raw_html = template.render(context)
    return transform(raw_html, remove_classes=False, disable_validation=True)


def _build_feedback_href() -> str:
    """Return a footer feedback destination from env, preferring explicit overrides."""
    feedback_url = os.getenv("FEEDBACK_URL", "").strip()
    if feedback_url:
        return feedback_url

    feedback_email = (
        os.getenv("FEEDBACK_EMAIL", "").strip()
        or os.getenv("EMAIL_FROM", "").strip()
        or DEFAULT_FEEDBACK_EMAIL
    )
    subject = quote(os.getenv("FEEDBACK_SUBJECT", DEFAULT_FEEDBACK_SUBJECT).strip())
    return f"mailto:{feedback_email}?subject={subject}"
