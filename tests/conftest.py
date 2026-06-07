"""Shared pytest fixtures (CLAUDE.md Section 3.8).

Mirrors the src structure under ``tests/``. Day-1 scope covers the core
infrastructure modules; broker/data-manager mocks are added on later days.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from core.logger import reset_logging


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    """Ensure each test starts and ends with no leftover root handlers."""
    reset_logging()
    yield
    reset_logging()
    logging.getLogger().setLevel(logging.WARNING)
