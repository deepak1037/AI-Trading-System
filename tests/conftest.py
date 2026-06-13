"""Shared pytest fixtures (CLAUDE.md Section 3.8).

Mirrors the src structure under ``tests/``. Day-1 scope covers the core
infrastructure modules; broker/data-manager mocks are added on later days.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from config.settings import settings
from core.logger import reset_logging


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    """Ensure each test starts and ends with no leftover root handlers."""
    reset_logging()
    yield
    reset_logging()
    logging.getLogger().setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the test suite hermetic: never call the real Anthropic API.

    A developer's ``.env`` carries a real ``ANTHROPIC_API_KEY`` (used by
    ``make analyze``), which would otherwise make LLM-backed code paths (earnings
    assessor, drop classifier, ROI analyzer) hit the network — slow and flaky.
    Blank it by default so those paths take their deterministic rule-based
    fallback; tests that exercise the LLM path explicitly set a key and mock the
    client, which overrides this.
    """
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
