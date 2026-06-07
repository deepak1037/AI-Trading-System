"""Centralised logging (CLAUDE.md Section 3.1).

Every module does exactly::

    from core.logger import get_logger
    logger = get_logger(__name__)

Output goes through the stdlib ``logging`` module to both a rotating file
handler (max 10 MB, 5 backups) and the console. ``LOG_LEVEL`` is read from
config and never hardcoded. Secrets must never be passed to the logger.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import settings

#: Format mandated by Section 3.1.
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5

#: Track which handlers we've installed so re-importing modules never stack
#: duplicate handlers on the root.
_configured = False


def _resolve_level() -> int:
    """Map the configured LOG_LEVEL string to a logging level int."""
    return getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)


def _build_file_handler() -> RotatingFileHandler:
    log_path = Path(settings.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    return handler


def _build_console_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    return handler


def _configure_root() -> None:
    """Install file + console handlers on the root logger exactly once."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(_resolve_level())

    # Remove any pre-existing handlers (e.g. from a prior basicConfig) so our
    # format is the only one in effect.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.addHandler(_build_file_handler())
    root.addHandler(_build_console_handler())

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for ``name`` (typically ``__name__``)."""
    _configure_root()
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level())
    return logger


def reset_logging() -> None:
    """Tear down installed handlers. Intended for tests only."""
    global _configured
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    _configured = False


__all__ = ["get_logger", "reset_logging"]
