from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from src.utils.config import AppConfig


def configure_logging(config: AppConfig) -> None:
    level = getattr(logging, config.settings.log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _ensure_required_fields,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.handlers.extend(_build_handlers(config, level, shared_processors))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            _ensure_required_fields,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, pipeline_stage: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name).bind(module=name, pipeline_stage=pipeline_stage)


def _build_handlers(
    config: AppConfig,
    level: int,
    shared_processors: list[Any],
) -> list[logging.Handler]:
    if level <= logging.DEBUG:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        return [console_handler]

    log_file = Path(config.settings.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(level)
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )
    return [file_handler]


def _ensure_required_fields(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    event_dict.setdefault("module", event_dict.get("logger", "unknown"))
    event_dict.setdefault("pipeline_stage", "general")
    return event_dict
