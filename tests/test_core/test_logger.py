"""Tests for core.logger — configured logging with rotation."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from core.logger import _BACKUP_COUNT, _MAX_BYTES, get_logger, reset_logging


def test_returns_logger_instance() -> None:
    logger = get_logger("test.module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test.module"


def test_installs_file_and_console_handlers() -> None:
    get_logger(__name__)
    root = logging.getLogger()
    types = [type(h) for h in root.handlers]
    assert RotatingFileHandler in types
    assert logging.StreamHandler in types


def test_rotation_configured_per_spec() -> None:
    get_logger(__name__)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert file_handlers, "expected a rotating file handler"
    handler = file_handlers[0]
    assert handler.maxBytes == _MAX_BYTES == 10 * 1024 * 1024
    assert handler.backupCount == _BACKUP_COUNT == 5


def test_handlers_not_duplicated_on_repeated_calls() -> None:
    get_logger("a")
    get_logger("b")
    get_logger("c")
    root = logging.getLogger()
    # Exactly one file + one console handler — never stacked.
    assert len(root.handlers) == 2


def test_format_matches_spec() -> None:
    get_logger(__name__)
    handler = logging.getLogger().handlers[0]
    assert handler.formatter is not None
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    formatted = handler.formatter.format(record)
    assert " | INFO     | x | hello" in formatted


def test_writes_to_log_file(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "sub" / "trading.log"
    monkeypatch.setattr("core.logger.settings.LOG_FILE", str(log_file))
    reset_logging()

    logger = get_logger("writer.test")
    logger.error("disk write check %s", 123)
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    contents = log_file.read_text()
    assert "disk write check 123" in contents
    assert "ERROR" in contents


def test_level_follows_config(monkeypatch) -> None:
    monkeypatch.setattr("core.logger.settings.LOG_LEVEL", "DEBUG")
    reset_logging()
    logger = get_logger("level.test")
    assert logger.level == logging.DEBUG
    assert logging.getLogger().level == logging.DEBUG
