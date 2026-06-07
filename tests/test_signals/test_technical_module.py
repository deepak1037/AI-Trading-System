"""Tests for signals/technical_module.py."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.exceptions import SignalError
from signals.technical_module import TechnicalModule


def _make_ohlcv(n: int = 250, trend: str = "up") -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    np.random.seed(42)
    prices = 100.0
    rows = []
    for i in range(n):
        drift = 0.002 if trend == "up" else -0.002
        change = drift + np.random.normal(0, 0.01)
        open_ = prices
        close = prices * (1 + change)
        high = max(open_, close) * (1 + abs(np.random.normal(0, 0.003)))
        low = min(open_, close) * (1 - abs(np.random.normal(0, 0.003)))
        volume = int(np.random.uniform(500_000, 2_000_000))
        rows.append({"open": open_, "high": high, "low": low, "close": close, "volume": volume})
        prices = close
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(rows, index=idx)


@pytest.fixture
def module():
    return TechnicalModule()


class TestTechnicalModule:
    def test_uptrend_returns_long(self, module):
        df = _make_ohlcv(250, trend="up")
        signal = module.score(df, ticker="TEST")
        assert signal.source == "technical"
        assert signal.direction in ("long", "strong_long", "neutral")

    def test_downtrend_returns_short(self, module):
        df = _make_ohlcv(250, trend="down")
        signal = module.score(df, ticker="TEST")
        assert signal.source == "technical"
        assert signal.direction in ("short", "strong_short", "neutral")

    def test_insufficient_data_raises(self, module):
        df = _make_ohlcv(20, trend="up")
        with pytest.raises(SignalError):
            module.score(df)

    def test_missing_columns_raises(self, module):
        df = _make_ohlcv(250, trend="up").drop(columns=["volume"])
        with pytest.raises(SignalError):
            module.score(df)

    def test_confidence_in_range(self, module):
        for trend in ("up", "down"):
            df = _make_ohlcv(250, trend=trend)
            signal = module.score(df)
            assert 0 <= signal.confidence <= 100

    def test_metadata_fields(self, module):
        df = _make_ohlcv(250)
        signal = module.score(df, ticker="SPY")
        assert "rsi" in signal.metadata
        assert "macd_hist" in signal.metadata
        assert "ma20" in signal.metadata
        assert "ma50" in signal.metadata
        assert "ma200" in signal.metadata
        assert signal.metadata["ticker"] == "SPY"

    def test_uppercase_columns_normalised(self, module):
        df = _make_ohlcv(250)
        df.columns = df.columns.str.upper()
        signal = module.score(df)
        assert signal.source == "technical"
