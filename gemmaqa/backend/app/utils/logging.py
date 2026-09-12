"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.utils.sanitization import sanitize_dict


class SanitizingFilter(logging.Filter):
    """Filter that redacts sensitive fields in log extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "payload") and isinstance(record.payload, dict):
            record.payload = sanitize_dict(record.payload)
        return True


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure root application logger."""
    logger = logging.getLogger("gemmaqa")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SanitizingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a child logger under the gemmaqa namespace."""
    base = logging.getLogger("gemmaqa")
    if not base.handlers:
        setup_logging()
    return base if not name else base.getChild(name)


def log_event(logger: logging.Logger, message: str, **payload: Any) -> None:
    """Log a structured event with optional sanitized payload."""
    if payload:
        logger.info("%s | payload=%s", message, sanitize_dict(payload))
    else:
        logger.info(message)
