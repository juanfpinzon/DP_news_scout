from __future__ import annotations

from src.storage.db import initialize_database
from src.utils.config import load_config
from src.utils.logging import configure_logging, get_logger


def main() -> None:
    config = load_config()
    configure_logging(config)
    logger = get_logger(__name__, pipeline_stage="bootstrap")

    initialize_database(config.settings.database_path)

    logger.info(
        "pipeline_bootstrap_complete",
        sources_configured=len(config.sources),
        recipients_configured=len(config.recipients),
        dry_run=config.settings.dry_run,
    )


if __name__ == "__main__":
    main()
