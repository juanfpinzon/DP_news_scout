"""Render a DPNS digest preview with realistic mock content.

Task 4.1.4 requires a script that can:
- build a representative digest payload,
- render both HTML and plain-text variants,
- save an HTML preview for browser inspection,
- optionally send the rendered email to a test recipient.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

try:
    from agentmail import AgentMail
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    AgentMail = None

from src.analyzer.digest import Digest, DigestItem, QuickHit
from src.renderer import render_digest, render_plaintext
from src.utils.config import DEFAULT_ENV_FILE

DEFAULT_PREVIEW_PATH = Path("/tmp/preview.html")
DEFAULT_SUBJECT_PREFIX = "Digital Procurement News Scout"


def build_mock_digest() -> Digest:
    """Return a realistic digest fixture for local renderer validation."""
    return Digest(
        top_story=DigestItem(
            url="https://example.com/news/procurement-ai-control-towers",
            headline="AI control towers move from pilot to production in source-to-pay",
            summary=(
                "Several enterprise platforms are packaging control-tower workflows "
                "that combine supplier signals, contract data, and risk monitoring "
                "into one operating view for procurement leaders."
            ),
            why_it_matters=(
                "Digital Procurement teams can use these operating layers to spot "
                "supply risk faster, automate follow-up actions, and scale decision "
                "support without adding manual reporting overhead."
            ),
            source="Procurement Technology Weekly",
            date="Apr 4, 2026",
        ),
        key_developments=[
            DigestItem(
                url="https://example.com/news/sap-joule-source-to-pay",
                headline="SAP extends Joule copilots into sourcing and supplier management",
                summary=(
                    "The update adds guided drafting, policy-aware recommendations, "
                    "and natural-language workflow support across sourcing events and "
                    "supplier interactions."
                ),
                why_it_matters=(
                    "This suggests source-to-pay suites are competing on embedded AI "
                    "productivity rather than standalone analytics."
                ),
                source="Enterprise Systems Journal",
                date="Apr 4, 2026",
            ),
            DigestItem(
                url="https://example.com/news/coupa-risk-integration",
                headline="Coupa links intake, risk, and savings tracking in a single release",
                summary=(
                    "The release ties business intake requests to supplier risk signals "
                    "and downstream value tracking so teams can see adoption and impact "
                    "inside one workflow."
                ),
                why_it_matters=(
                    "Cross-functional visibility is critical if Procurement wants to "
                    "prove value beyond negotiated price reductions."
                ),
                source="Future of Procurement",
                date="Apr 3, 2026",
            ),
            DigestItem(
                url="https://example.com/news/orchestration-supplier-onboarding",
                headline="Orchestration vendors target supplier onboarding bottlenecks",
                summary=(
                    "New tooling focuses on handoffs between procurement, legal, risk, "
                    "and finance teams to reduce cycle time for supplier activation."
                ),
                why_it_matters=(
                    "This is directly relevant to operating-model simplification and "
                    "faster realization of sourcing decisions."
                ),
                source="CPO Digital",
                date="Apr 2, 2026",
            ),
        ],
        on_our_radar=[
            DigestItem(
                url="https://example.com/news/esg-data-supplier-scorecards",
                headline="Supplier scorecards add emissions and resilience data at category level",
                summary=(
                    "Providers are blending ESG indicators with operational performance "
                    "metrics so category managers can compare suppliers on a broader "
                    "set of trade-offs."
                ),
                why_it_matters=(
                    "Broader scorecards can help Digital Procurement connect sourcing "
                    "choices with resilience and sustainability priorities."
                ),
                source="Spend Matters",
                date="Apr 3, 2026",
            ),
            DigestItem(
                url="https://example.com/news/genai-procurement-governance",
                headline="Procurement leaders formalize GenAI governance for frontline teams",
                summary=(
                    "More organizations are publishing approved use cases, review steps, "
                    "and prompt guardrails for sourcing and contract management teams."
                ),
                why_it_matters=(
                    "Governance maturity is becoming a prerequisite for scaling AI "
                    "adoption in enterprise procurement."
                ),
                source="Digital Operations Review",
                date="Apr 1, 2026",
            ),
        ],
        quick_hits=[
            QuickHit(
                url="https://example.com/news/erp-modernization",
                one_liner="ERP modernization budgets keep favoring procurement workflow simplification",
                source="CIO Strategy Daily",
            ),
            QuickHit(
                url="https://example.com/news/supplier-diversity-data",
                one_liner="Supplier diversity reporting tools add cleaner data normalization",
                source="Supply Chain Dive",
            ),
            QuickHit(
                url="https://example.com/news/contract-ai-redlining",
                one_liner="Contract AI vendors pitch redlining copilots with tighter approval controls",
                source="Legal Tech Monitor",
            ),
        ],
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the Digital Procurement News Scout email with mock data.",
    )
    parser.add_argument(
        "--preview-path",
        default=str(DEFAULT_PREVIEW_PATH),
        help="Where to save the rendered HTML preview. Default: /tmp/preview.html",
    )
    parser.add_argument(
        "--plaintext-path",
        help="Optional path for the rendered plain-text preview. Defaults next to the HTML file.",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=1,
        help="Issue number shown in the header and footer.",
    )
    parser.add_argument(
        "--date",
        help='Digest date label. Defaults to today, formatted like "April 4, 2026".',
    )
    parser.add_argument(
        "--to",
        help="Optional test recipient. When provided, the script sends the preview via AgentMail.",
    )
    parser.add_argument(
        "--subject",
        help="Optional subject override for the sent test email.",
    )
    return parser.parse_args(argv)


def save_preview(content: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def send_test_email(
    *,
    recipient: str,
    subject: str,
    html: str,
    plaintext: str,
) -> None:
    if AgentMail is None:
        raise RuntimeError("agentmail is not installed; cannot send test email")

    api_key = _require_env("AGENTMAIL_API_KEY")
    inbox_id = _require_env("AGENTMAIL_INBOX_ID")
    reply_to = os.getenv("EMAIL_FROM", "").strip()

    client = AgentMail(api_key=api_key)
    send_kwargs = {
        "to": recipient,
        "subject": subject,
        "html": html,
        "text": plaintext,
    }
    if reply_to:
        send_kwargs["reply_to"] = reply_to

    client.inboxes.messages.send(inbox_id, **send_kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(DEFAULT_ENV_FILE)

    args = parse_args(argv)
    digest = build_mock_digest()
    issue_number = args.issue_number
    date_label = args.date or _format_display_date(datetime.now())
    preview_path = Path(args.preview_path).expanduser()
    plaintext_path = (
        Path(args.plaintext_path).expanduser()
        if args.plaintext_path
        else _default_plaintext_path(preview_path)
    )

    html = render_digest(digest, issue_number=issue_number, date=date_label)
    plaintext = render_plaintext(digest, issue_number, date_label)

    save_preview(html, preview_path)
    save_preview(plaintext, plaintext_path)

    print(f"HTML preview saved to {preview_path}")
    print(f"Plain-text preview saved to {plaintext_path}")

    if args.to:
        subject = args.subject or _build_default_subject(issue_number=issue_number, date=date_label)
        send_test_email(
            recipient=args.to,
            subject=subject,
            html=html,
            plaintext=plaintext,
        )
        print(f"Sent test digest to {args.to}")

    return 0


def _build_default_subject(*, issue_number: int, date: str) -> str:
    return f"{DEFAULT_SUBJECT_PREFIX} | {date} | Issue #{issue_number}"


def _default_plaintext_path(preview_path: Path) -> Path:
    if preview_path.suffix:
        return preview_path.with_suffix(".txt")
    return Path(f"{preview_path}.txt")


def _format_display_date(current_time: datetime) -> str:
    return current_time.strftime("%B ") + str(current_time.day) + current_time.strftime(", %Y")


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{key}' for sending test email",
        )
    return value


if __name__ == "__main__":
    raise SystemExit(main())
