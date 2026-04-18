from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

try:
    from agentmail import AgentMail
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    AgentMail = None

from src.storage.db import DeliveryRecord, has_successful_delivery, log_delivery, utc_now_iso
from src.utils.config import AppConfig, RecipientConfig, load_config
from src.utils.logging import get_logger
from src.utils.progress import emit_progress

MAX_SEND_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0


def send_digest(
    html: str,
    plaintext: str,
    subject: str,
    *,
    config: AppConfig | None = None,
    run_id: int | None = None,
    issue_number: int | None = None,
    group: str | None = None,
    client: Any | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """Send a rendered digest email through AgentMail."""
    config = config or load_config()
    logger = get_logger(__name__, pipeline_stage="sender")

    recipients = _resolve_recipients(config, group=group)
    recipient_emails = [recipient.email for recipient in recipients]
    recipient_count = len(recipient_emails)
    active_group = group or config.default_recipient_group
    idempotency_key = _build_idempotency_key(
        issue_number=issue_number,
        subject=subject,
        recipient_group=active_group,
        recipient_emails=recipient_emails,
    )

    if recipient_count == 0:
        message = f"No recipients configured for group '{active_group}'"
        logger.warning(
            "email_delivery_skipped",
            recipient_group=active_group,
            recipient_count=0,
            subject=subject,
            reason=message,
        )
        _record_delivery(
            config=config,
            run_id=run_id,
            recipient_count=0,
            status="skipped",
            error=message,
            idempotency_key=idempotency_key,
        )
        emit_progress(progress_callback, message)
        return False

    if has_successful_delivery(
        config.settings.database_path,
        idempotency_key=idempotency_key,
    ):
        logger.info(
            "email_delivery_idempotent_skip",
            recipient_group=active_group,
            recipient_count=recipient_count,
            subject=subject,
            idempotency_key=idempotency_key,
        )
        _record_delivery(
            config=config,
            run_id=run_id,
            recipient_count=recipient_count,
            status="idempotent_skip",
            error=None,
            idempotency_key=idempotency_key,
        )
        emit_progress(
            progress_callback,
            "Email delivery skipped because this digest was already sent.",
        )
        return True

    if client is None:
        if AgentMail is None:
            raise RuntimeError("agentmail is not installed; cannot send digest")
        client = AgentMail(api_key=config.env.agentmail_api_key)

    send_kwargs = {
        "to": config.env.email_from,
        "bcc": recipient_emails,
        "subject": subject,
        "html": html,
        "text": plaintext,
        "reply_to": config.env.email_from,
    }

    last_error: str | None = None
    last_error_type: str | None = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        emit_progress(
            progress_callback,
            f"Email delivery attempt {attempt}/{MAX_SEND_ATTEMPTS} "
            f"to {recipient_count} {_pluralize(recipient_count, 'recipient')}.",
        )
        try:
            response = client.inboxes.messages.send(
                config.env.agentmail_inbox_id,
                **send_kwargs,
            )
            logger.info(
                "email_delivery_succeeded",
                recipient_group=active_group,
                recipient_count=recipient_count,
                attempt=attempt,
                subject=subject,
                message_id=getattr(response, "id", None),
            )
            _record_delivery(
                config=config,
                run_id=run_id,
                recipient_count=recipient_count,
                status="sent",
                error=None,
                idempotency_key=idempotency_key,
            )
            emit_progress(
                progress_callback,
                f"Email delivery succeeded on attempt {attempt}.",
            )
            return True
        except Exception as exc:  # pragma: no cover - SDK exception types vary
            last_error = str(exc)
            last_error_type = type(exc).__name__
            logger.warning(
                "email_delivery_attempt_failed",
                recipient_group=active_group,
                recipient_count=recipient_count,
                attempt=attempt,
                max_attempts=MAX_SEND_ATTEMPTS,
                subject=subject,
                error=last_error,
                error_type=last_error_type,
            )
            if attempt < MAX_SEND_ATTEMPTS:
                delay_seconds = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                emit_progress(
                    progress_callback,
                    f"Email delivery attempt {attempt}/{MAX_SEND_ATTEMPTS} failed "
                    f"({last_error_type}); retrying in {delay_seconds:.1f}s.",
                )
                sleep_fn(delay_seconds)

    logger.error(
        "email_delivery_failed",
        recipient_group=active_group,
        recipient_count=recipient_count,
        subject=subject,
        error=last_error,
        error_type=last_error_type,
    )
    _record_delivery(
        config=config,
        run_id=run_id,
        recipient_count=recipient_count,
        status="failed",
        error=last_error,
        idempotency_key=idempotency_key,
    )
    emit_progress(
        progress_callback,
        "Email delivery failed after "
        f"{MAX_SEND_ATTEMPTS} attempts: {last_error or 'unknown error'}.",
    )
    return False


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def _resolve_recipients(config: AppConfig, *, group: str | None) -> list[RecipientConfig]:
    group_name = group or config.default_recipient_group
    recipients = config.recipient_groups.get(group_name)
    if recipients is None:
        raise ValueError(f"Unknown recipient group '{group_name}'")
    return _deduplicate_recipients(recipients)


def _deduplicate_recipients(recipients: list[RecipientConfig]) -> list[RecipientConfig]:
    unique_recipients: list[RecipientConfig] = []
    seen: set[str] = set()

    for recipient in recipients:
        normalized_email = recipient.email.strip().lower()
        if normalized_email in seen:
            continue
        seen.add(normalized_email)
        unique_recipients.append(recipient)

    return unique_recipients


def _build_idempotency_key(
    *,
    issue_number: int | None,
    subject: str,
    recipient_group: str,
    recipient_emails: list[str],
) -> str:
    digest_identity = (
        f"issue:{issue_number}"
        if issue_number is not None
        else f"subject:{subject.strip()}"
    )
    digest_source = "|".join(
        [
            digest_identity,
            recipient_group.strip().lower(),
            ",".join(sorted(email.strip().lower() for email in recipient_emails)),
        ]
    )
    return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()


def _record_delivery(
    *,
    config: AppConfig,
    run_id: int | None,
    recipient_count: int,
    status: str,
    error: str | None,
    idempotency_key: str | None,
) -> None:
    if run_id is None:
        return

    log_delivery(
        config.settings.database_path,
        DeliveryRecord(
            run_id=run_id,
            sent_at=utc_now_iso(),
            recipient_count=recipient_count,
            status=status,
            error=error,
            idempotency_key=idempotency_key,
        ),
    )
