"""Application logging with secret redaction.

Logs live in the per-user data directory and are rotated.  Command output is
never written here in full; the audit database stores redacted summaries and
details.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from winfirst.core.paths import log_file
from winfirst.core.redaction import redact_secrets

_LOGGER_NAME = "winfirst"
_configured = False


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def setup_logging(level: int = logging.INFO, console: bool | None = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    try:
        handler = RotatingFileHandler(
            log_file(), maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError:
        pass
    if console is None:
        console = not getattr(sys, "frozen", False)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(logging.WARNING)
        logger.addHandler(stream)
    logging.getLogger("PySide6").setLevel(logging.WARNING)
    _configured = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
