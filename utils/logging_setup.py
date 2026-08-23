"""Idempotent process-wide logging setup for every ATLAS entry point."""

from __future__ import annotations

from datetime import datetime
import logging
import logging.handlers
from pathlib import Path

import config
from utils.helpers import CredentialFilter, get_logs_dir, restrict_path_to_owner


def configure_atlas_logging() -> Path:
    """Configure redacted console and rotating-file sinks exactly once."""

    root_logger = logging.getLogger()
    root_logger.setLevel(config.LOG_LEVEL)
    formatter = logging.Formatter(config.LOG_FORMAT)

    existing_file = next(
        (
            handler
            for handler in root_logger.handlers
            if getattr(handler, "_atlas_file_handler", False)
        ),
        None,
    )
    existing_console = next(
        (
            handler
            for handler in root_logger.handlers
            if getattr(handler, "_atlas_console_handler", False)
        ),
        None,
    )

    if existing_console is None:
        console_handler = logging.StreamHandler()
        console_handler._atlas_console_handler = True
        console_handler.setFormatter(formatter)
        console_handler.addFilter(CredentialFilter())
        root_logger.addHandler(console_handler)

    if existing_file is not None:
        return Path(existing_file.baseFilename)

    log_dir = Path(get_logs_dir())
    restrict_path_to_owner(log_dir, is_dir=True)
    log_file = log_dir / datetime.now().strftime(
        "ATLAS_%Y-%m-%d_%H-%M-%S.log"
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=getattr(config, "LOG_MAX_BYTES", 5 * 1024 * 1024),
        backupCount=getattr(config, "LOG_BACKUP_COUNT", 5),
        encoding="utf-8",
    )
    file_handler._atlas_file_handler = True
    file_handler.setFormatter(formatter)
    file_handler.addFilter(CredentialFilter())
    root_logger.addHandler(file_handler)
    return log_file


__all__ = ["configure_atlas_logging"]
