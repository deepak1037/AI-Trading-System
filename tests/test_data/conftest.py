"""Shared fixtures for the data layer test suite."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from data.db import init_db


@pytest.fixture
def tmp_db(tmp_path: pytest.TempPathFactory) -> str:
    """Initialise a fresh SQLite database in a temp dir; return its path."""
    db_path = str(tmp_path / "test_trading.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def sample_ohlcv_df() -> pd.DataFrame:
    """Minimal OHLCV DataFrame as returned by ``YFinanceConnector._normalise``."""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    return pd.DataFrame(
        {
            "open": [180.00, 182.00, 181.00],
            "high": [185.00, 184.00, 183.00],
            "low": [179.00, 181.00, 180.00],
            "close": [184.00, 183.00, 182.00],
            "volume": [50_000_000, 48_000_000, 52_000_000],
        },
        index=dates,
    )
