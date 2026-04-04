from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.utils.source_validation import validate_source_payload

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(slots=True)
class Settings:
    max_articles_per_source: int
    max_digest_items: int
    relevance_threshold: int
    digest_send_time: str
    timezone: str
    llm_model: str
    llm_model_fallback: str
    database_path: str
    log_level: str
    log_file: str
    dry_run: bool
    pipeline_timeout: int
    fetch_concurrency: int
    rss_lookback_hours: int
    dedup_window_days: int
    request_timeout_seconds: float
    rate_limit_seconds: float
    max_digest_items_per_source: int = 3
    email_max_width_px: int = 880
    issue_number_override: int | None = None


@dataclass(slots=True)
class SourceConfig:
    name: str
    url: str
    tier: int | None = None
    method: str | None = None
    active: bool = True
    category: str | None = None
    selectors: dict[str, Any] | None = None


@dataclass(slots=True)
class RecipientConfig:
    email: str
    name: str | None = None


@dataclass(slots=True)
class EnvConfig:
    openrouter_api_key: str
    agentmail_api_key: str
    agentmail_inbox_id: str
    email_from: str


@dataclass(slots=True)
class AppConfig:
    settings: Settings
    env: EnvConfig
    sources: list[SourceConfig]
    recipients: list[RecipientConfig]
    recipient_groups: dict[str, list[RecipientConfig]]
    default_recipient_group: str


def load_config(env_file: Path | None = None) -> AppConfig:
    load_dotenv(env_file or DEFAULT_ENV_FILE, override=env_file is not None)

    settings_data = {
        "max_digest_items_per_source": 3,
        "email_max_width_px": 880,
        "issue_number_override": None,
        **_read_yaml(CONFIG_DIR / "settings.yaml"),
    }
    sources_data = _read_yaml(CONFIG_DIR / "sources.yaml")
    recipients_data = _read_yaml(CONFIG_DIR / "recipients.yaml")

    settings = Settings(
        max_articles_per_source=_get_int(
            "MAX_ARTICLES_PER_SOURCE",
            settings_data,
            "max_articles_per_source",
        ),
        max_digest_items=_get_int("MAX_DIGEST_ITEMS", settings_data, "max_digest_items"),
        relevance_threshold=_get_int(
            "RELEVANCE_THRESHOLD",
            settings_data,
            "relevance_threshold",
        ),
        digest_send_time=_get_str("DIGEST_SEND_TIME", settings_data, "digest_send_time"),
        timezone=_get_str("TIMEZONE", settings_data, "timezone"),
        llm_model=_get_str("LLM_MODEL", settings_data, "llm_model"),
        llm_model_fallback=_get_str(
            "LLM_MODEL_FALLBACK",
            settings_data,
            "llm_model_fallback",
        ),
        database_path=_get_str("DPNS_DATABASE_PATH", settings_data, "database_path"),
        log_level=_get_str("LOG_LEVEL", settings_data, "log_level").upper(),
        log_file=_get_str("LOG_FILE", settings_data, "log_file"),
        dry_run=_get_bool("DRY_RUN", settings_data, "dry_run"),
        pipeline_timeout=_get_int("PIPELINE_TIMEOUT", settings_data, "pipeline_timeout"),
        fetch_concurrency=_get_int(
            "FETCH_CONCURRENCY",
            settings_data,
            "fetch_concurrency",
        ),
        rss_lookback_hours=_get_int(
            "RSS_LOOKBACK_HOURS",
            settings_data,
            "rss_lookback_hours",
        ),
        dedup_window_days=_get_int(
            "DEDUP_WINDOW_DAYS",
            settings_data,
            "dedup_window_days",
        ),
        request_timeout_seconds=_get_float(
            "REQUEST_TIMEOUT_SECONDS",
            settings_data,
            "request_timeout_seconds",
        ),
        rate_limit_seconds=_get_float(
            "RATE_LIMIT_SECONDS",
            settings_data,
            "rate_limit_seconds",
        ),
        max_digest_items_per_source=_get_int(
            "MAX_DIGEST_ITEMS_PER_SOURCE",
            settings_data,
            "max_digest_items_per_source",
        ),
        email_max_width_px=_get_int(
            "EMAIL_MAX_WIDTH_PX",
            settings_data,
            "email_max_width_px",
        ),
        issue_number_override=_get_optional_int(
            "ISSUE_NUMBER_OVERRIDE",
            settings_data,
            "issue_number_override",
        ),
    )
    _validate_settings(settings)

    env = EnvConfig(
        openrouter_api_key=_require_env("OPENROUTER_API_KEY"),
        agentmail_api_key=_require_env("AGENTMAIL_API_KEY"),
        agentmail_inbox_id=_require_env("AGENTMAIL_INBOX_ID"),
        email_from=_require_env("EMAIL_FROM"),
    )

    sources = _build_sources(sources_data)
    recipient_groups, default_recipient_group = _build_recipient_groups(recipients_data)
    recipients = list(recipient_groups[default_recipient_group])

    return AppConfig(
        settings=settings,
        env=env,
        sources=sources,
        recipients=recipients,
        recipient_groups=recipient_groups,
        default_recipient_group=default_recipient_group,
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file must contain a mapping: {path}")
        return loaded


def _get_str(env_key: str, config: dict[str, Any], config_key: str) -> str:
    value = os.getenv(env_key, config.get(config_key))
    if value is None or str(value).strip() == "":
        raise ConfigError(f"Missing required config value for '{config_key}'")
    return str(value).strip()


def _get_int(env_key: str, config: dict[str, Any], config_key: str) -> int:
    value = os.getenv(env_key, config.get(config_key))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for '{config_key}': {value!r}") from exc


def _get_bool(env_key: str, config: dict[str, Any], config_key: str) -> bool:
    value = os.getenv(env_key)
    if value is None:
        raw = config.get(config_key)
        if isinstance(raw, bool):
            return raw
        if raw is None:
            raise ConfigError(f"Missing required config value for '{config_key}'")
        value = str(raw)

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid boolean for '{config_key}': {value!r}")


def _get_optional_int(env_key: str, config: dict[str, Any], config_key: str) -> int | None:
    value = os.getenv(env_key)
    if value is None:
        value = config.get(config_key)

    if value is None:
        return None

    normalized = str(value).strip()
    if normalized == "" or normalized.lower() in {"none", "null"}:
        return None

    try:
        return int(normalized)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for '{config_key}': {value!r}") from exc


def _get_float(env_key: str, config: dict[str, Any], config_key: str) -> float:
    value = os.getenv(env_key, config.get(config_key))
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid float for '{config_key}': {value!r}") from exc


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        raise ConfigError(f"Missing required environment variable '{key}'")
    return value.strip()


def _validate_settings(settings: Settings) -> None:
    if settings.max_articles_per_source <= 0:
        raise ConfigError("max_articles_per_source must be greater than 0")
    if settings.max_digest_items <= 0:
        raise ConfigError("max_digest_items must be greater than 0")
    if not 1 <= settings.relevance_threshold <= 10:
        raise ConfigError("relevance_threshold must be between 1 and 10")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", settings.digest_send_time):
        raise ConfigError("digest_send_time must use HH:MM format")
    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("log_level must be a valid Python logging level")
    if settings.pipeline_timeout <= 0:
        raise ConfigError("pipeline_timeout must be greater than 0")
    if settings.fetch_concurrency <= 0:
        raise ConfigError("fetch_concurrency must be greater than 0")
    if settings.rss_lookback_hours <= 0:
        raise ConfigError("rss_lookback_hours must be greater than 0")
    if settings.dedup_window_days <= 0:
        raise ConfigError("dedup_window_days must be greater than 0")
    if settings.request_timeout_seconds <= 0:
        raise ConfigError("request_timeout_seconds must be greater than 0")
    if settings.rate_limit_seconds < 0:
        raise ConfigError("rate_limit_seconds must be 0 or greater")
    if settings.max_digest_items_per_source <= 0:
        raise ConfigError("max_digest_items_per_source must be greater than 0")
    if settings.email_max_width_px <= 0:
        raise ConfigError("email_max_width_px must be greater than 0")
    if settings.issue_number_override is not None and settings.issue_number_override < 0:
        raise ConfigError("issue_number_override must be 0 or greater")


def _build_sources(config: dict[str, Any]) -> list[SourceConfig]:
    raw_sources = config.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("sources.yaml must contain a 'sources' list")

    sources: list[SourceConfig] = []
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"sources[{index}] must be a mapping")

        normalized = validate_source_payload(item, index=index, error_cls=ConfigError)
        sources.append(SourceConfig(**normalized))
    return sources


def _build_recipient_groups(
    config: dict[str, Any],
) -> tuple[dict[str, list[RecipientConfig]], str]:
    raw_groups = config.get("groups")
    raw_recipients = config.get("recipients")

    if raw_groups is not None:
        if raw_recipients is not None:
            raise ConfigError(
                "recipients.yaml cannot define both 'groups' and 'recipients'"
            )
        if not isinstance(raw_groups, dict):
            raise ConfigError("recipients.yaml 'groups' must be a mapping")

        recipient_groups: dict[str, list[RecipientConfig]] = {}
        for group_name, group_recipients in raw_groups.items():
            if not _is_non_empty_string(group_name):
                raise ConfigError("recipient group names must be non-empty strings")
            recipient_groups[group_name] = _build_recipients(
                group_recipients,
                section=f"groups.{group_name}",
            )

        if not recipient_groups:
            raise ConfigError("recipients.yaml 'groups' must define at least one group")

        default_group = str(
            config.get(
                "default_group",
                "leadership" if "leadership" in recipient_groups else next(iter(recipient_groups)),
            )
        ).strip()
        if default_group not in recipient_groups:
            raise ConfigError(
                f"default_group '{default_group}' is not defined in recipients.yaml"
            )
        return recipient_groups, default_group

    recipient_groups = {
        str(config.get("default_group", "default")).strip() or "default": _build_recipients(
            raw_recipients if raw_recipients is not None else [],
            section="recipients",
        )
    }
    default_group = next(iter(recipient_groups))
    return recipient_groups, default_group


def _build_recipients(raw_recipients: Any, *, section: str) -> list[RecipientConfig]:
    if not isinstance(raw_recipients, list):
        raise ConfigError(f"{section} must be a list")

    recipients: list[RecipientConfig] = []
    for index, item in enumerate(raw_recipients, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"{section}[{index}] must be a mapping")

        recipient = RecipientConfig(**item)
        if not _is_non_empty_string(recipient.email):
            raise ConfigError(f"{section}[{index}].email is required")
        recipients.append(recipient)
    return recipients


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""
