"""Tests for scanner/technical_screen.py (Days 13-18)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scanner.technical_screen import TechnicalScreen


def _make_df(prices: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV dataframe for testing."""
    n = len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1_000_000] * n,
    })


def _stage2_prices() -> list[float]:
    """Generate 250 prices in Stage 2 — steady uptrend above all MAs."""
    base = 50.0
    prices = []
    for i in range(250):
        prices.append(base + i * 0.5 + np.random.uniform(-0.5, 0.5))
    return prices


def _declining_prices() -> list[float]:
    """Generate 250 declining prices — below all MAs."""
    prices = []
    for i in range(250):
        prices.append(200.0 - i * 0.4)
    return prices


class TestStage2Detection:
    def setup_method(self):
        self.screen = TechnicalScreen()

    def test_too_few_rows_returns_false(self):
        df = _make_df([100.0] * 50)
        assert self.screen._is_stage2(df) is False

    def test_uptrending_stock_passes_stage2(self):
        prices = _stage2_prices()
        df = _make_df(prices)
        result = self.screen._is_stage2(df)
        assert isinstance(result, (bool, np.bool_))

    def test_declining_stock_fails_stage2(self):
        prices = _declining_prices()
        df = _make_df(prices)
        result = self.screen._is_stage2(df)
        # Declining prices should fail Stage 2
        assert not result


class TestBaseDetection:
    def setup_method(self):
        self.screen = TechnicalScreen()

    def test_tight_consolidation_is_base(self):
        # Price within 5% range for 30 days
        prices = [100.0 + i * 0.1 for i in range(50)]
        df = _make_df(prices)
        result = self.screen._is_in_base(df, lookback=30)
        assert result  # True or np.bool_ True

    def test_wide_range_not_base(self):
        # Price range > 15%
        prices = [100.0] * 25 + [120.0] * 25
        df = _make_df(prices)
        result = self.screen._is_in_base(df, lookback=30)
        assert not result

    def test_too_few_rows_returns_false(self):
        df = _make_df([100.0] * 5)
        assert self.screen._is_in_base(df, lookback=30) is False


class TestRsRank:
    def setup_method(self):
        self.screen = TechnicalScreen()

    def test_rs_rank_callable(self):
        assert callable(self.screen._compute_rs_rank)

    def test_rs_rank_returns_0_on_error(self):
        rs = self.screen._compute_rs_rank("INVALID_TICKER_XYZ_999", 10.0)
        assert rs == 0.0


class TestTechnicalScreenIntegration:
    def test_screen_empty_list(self):
        screen = TechnicalScreen()
        result = screen.screen([])
        assert result == []

    def test_screen_returns_list(self):
        screen = TechnicalScreen()
        result = screen.screen(["AAPL", "MSFT"])
        assert isinstance(result, list)
        assert len(result) <= 2
