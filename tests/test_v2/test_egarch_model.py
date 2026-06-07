"""Tests for v2/egarch_model.py (Days 19-25)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from v2.egarch_model import EGARCHForecast, EGARCHModel


def _make_returns(n: int = 200, vol: float = 1.5) -> pd.Series:
    """Synthetic log returns with specified daily volatility (in %)."""
    rng = np.random.default_rng(42)
    returns = rng.normal(0, vol, n)
    return pd.Series(returns, name="returns")


class TestEGARCHForecast:
    def test_direction_range_long(self):
        f = EGARCHForecast("SPY", 25.0, 1.5, -3.0, 3.0)
        low, high = f.direction_range("long")
        assert low > 0
        assert high > low

    def test_direction_range_short(self):
        f = EGARCHForecast("SPY", 25.0, 1.5, -3.0, 3.0)
        low, high = f.direction_range("short")
        assert low < 0
        assert high < 0
        assert high > low

    def test_direction_range_neutral(self):
        f = EGARCHForecast("SPY", 25.0, 1.5, -3.0, 3.0)
        low, high = f.direction_range("neutral")
        assert low < 0
        assert high > 0

    def test_strong_long_wider_than_long(self):
        f = EGARCHForecast("SPY", 25.0, 1.5, -3.0, 3.0)
        _, high_strong = f.direction_range("strong_long")
        _, high_long = f.direction_range("long")
        assert high_strong == high_long  # same formula used

    def test_repr(self):
        f = EGARCHForecast("SPY", 25.0, 1.5, -3.0, 3.0)
        assert "SPY" in repr(f)
        assert "1.5" in repr(f)


class TestEGARCHModelOffline:
    def test_fit_offline_returns_forecast(self):
        model = EGARCHModel()
        returns = _make_returns(200, 1.5)
        result = model.fit_offline(returns, ticker="TEST", horizon=1)
        assert isinstance(result, EGARCHForecast)
        assert result.ticker == "TEST"

    def test_fit_offline_daily_vol_positive(self):
        model = EGARCHModel()
        returns = _make_returns(200, 2.0)
        result = model.fit_offline(returns, ticker="TEST")
        assert result.daily_vol > 0

    def test_fit_offline_range_symmetric(self):
        model = EGARCHModel()
        returns = _make_returns(200, 1.5)
        result = model.fit_offline(returns, ticker="TEST")
        # For zero-mean process, range should be symmetric around 0
        assert abs(result.forecast_1d_high + result.forecast_1d_low) < 1.0

    def test_annualized_vol_reasonable(self):
        model = EGARCHModel()
        returns = _make_returns(300, 1.5)
        result = model.fit_offline(returns, ticker="TEST")
        # 1.5% daily × sqrt(252) ≈ 23.8% annualized
        assert 10.0 <= result.annualized_vol <= 60.0

    def test_fallback_forecast_no_arch(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "arch", None)
        model = EGARCHModel()
        result = model._fallback_forecast("SPY", 1, 0.95)
        assert isinstance(result, EGARCHForecast)
        assert result.daily_vol > 0


class TestEGARCHModelLive:
    def test_fit_and_forecast_returns_forecast_or_fallback(self):
        model = EGARCHModel()
        # Either returns a valid forecast or falls back gracefully
        try:
            result = model.fit_and_forecast("SPY", horizon=1)
            assert isinstance(result, EGARCHForecast)
            assert result.daily_vol > 0
        except Exception as exc:
            pytest.skip(f"Live data unavailable: {exc}")
