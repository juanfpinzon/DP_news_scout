from __future__ import annotations

DEFAULT_SUBJECT_PREFIX = "Digital Procurement News Scout"


def build_digest_subject(
    *,
    issue_number: int,
    date_label: str,
    subject_suffix: str | None = None,
) -> str:
    subject = f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | Issue #{issue_number}"
    return append_subject_suffix(subject, subject_suffix)


def build_no_news_subject(
    *,
    issue_number: int,
    date_label: str,
    subject_suffix: str | None = None,
) -> str:
    subject = (
        f"{DEFAULT_SUBJECT_PREFIX} | {date_label} | No major updates | Issue #{issue_number}"
    )
    return append_subject_suffix(subject, subject_suffix)


def append_subject_suffix(subject: str, subject_suffix: str | None) -> str:
    if not subject_suffix:
        return subject
    return f"{subject} | {subject_suffix}"
