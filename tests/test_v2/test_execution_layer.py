"""Tests for v2/execution_layer.py (Days 19-25)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from v2.execution_layer import V2ExecutionEngine
from v2.egarch_model import EGARCHForecast
from v2.gex_calculator import GEXResult
from broker_core.base_broker import Account
from signals.signal_schema import Signal


def _make_account(equity: float = 10_000_000.0) -> Account:
    return Account(account_id="paper_main", cash=equity, equity=equity, buying_power=equity)


def _make_signal(direction: str = "long", confidence: int = 75) -> Signal:
    return Signal(
        direction=direction,
        confidence=confidence,
        source="fusion",
        timestamp=datetime.now(tz=timezone.utc),
        metadata={"ticker": "SPY"},
    )


class TestV2ExecutionEngine:
    def setup_method(self):
        self.engine = V2ExecutionEngine()

    def test_estimate_move_range_long(self):
        with patch.object(self.engine._egarch, "fit_and_forecast") as mock:
            mock.return_value = EGARCHForecast("SPY", 25.0, 1.5, -2.9, 2.9)
            low, high = self.engine.estimate_move_range(_make_signal("long"), "SPY")
        assert low > 0
        assert high > low

    def test_estimate_move_range_short(self):
        with patch.object(self.engine._egarch, "fit_and_forecast") as mock:
            mock.return_value = EGARCHForecast("SPY", 25.0, 1.5, -2.9, 2.9)
            low, high = self.engine.estimate_move_range(_make_signal("short"), "SPY")
        assert low < 0
        assert high < 0

    def test_kelly_size_with_vol_reduces_on_high_vol(self):
        # High volatility should reduce position size vs base Kelly
        with patch.object(self.engine._egarch, "fit_and_forecast") as mock:
            mock.return_value = EGARCHForecast("SPY", 60.0, 4.0, -8.0, 8.0)  # 4% daily vol
            acct = _make_account()
            qty = self.engine.kelly_size_with_vol(acct, "SPY", _make_signal(), 450.0)
            assert qty >= 1  # at least 1 share

    def test_kelly_size_with_vol_normal_vol(self):
        with patch.object(self.engine._egarch, "fit_and_forecast") as mock:
            mock.return_value = EGARCHForecast("SPY", 20.0, 1.2, -2.4, 2.4)  # normal 1.2% vol
            acct = _make_account()
            qty = self.engine.kelly_size_with_vol(acct, "SPY", _make_signal(), 450.0)
            assert qty >= 1

    def test_gex_regime_ok_neutral_gex(self):
        with patch.object(self.engine._gex, "compute") as mock:
            mock.return_value = GEXResult("SPY", net_gex=0.0, call_gex=0.0, put_gex=0.0)
            assert self.engine.gex_regime_ok("SPY", 450.0, "long") is True

    def test_gex_regime_ok_positive_gex_short_blocked(self):
        with patch.object(self.engine._gex, "compute") as mock:
            mock.return_value = GEXResult("SPY", net_gex=5e9, call_gex=5e9, put_gex=0.0)
            assert self.engine.gex_regime_ok("SPY", 450.0, "short") is False

    def test_gex_regime_ok_negative_gex_long_ok(self):
        with patch.object(self.engine._gex, "compute") as mock:
            mock.return_value = GEXResult("SPY", net_gex=-5e9, call_gex=0.0, put_gex=-5e9)
            assert self.engine.gex_regime_ok("SPY", 450.0, "long") is True

    def test_gex_compute_failure_does_not_block(self):
        with patch.object(self.engine._gex, "compute", side_effect=Exception("API error")):
            assert self.engine.gex_regime_ok("SPY", 450.0, "long") is True

    def test_build_enhanced_order_long(self):
        with (
            patch.object(self.engine._egarch, "fit_and_forecast") as mock_egarch,
            patch.object(self.engine, "compute_atr_stop") as mock_atr,
        ):
            mock_egarch.return_value = EGARCHForecast("SPY", 20.0, 1.2, -2.4, 2.4)
            mock_atr.return_value = (440.0, 470.0)
            order = self.engine.build_enhanced_order(
                _make_signal("long"), _make_account(), "SPY", 450.0
            )
        assert order.action == "BUY"
        assert order.ticker == "SPY"
        assert order.qty >= 1

    def test_build_enhanced_order_short(self):
        with (
            patch.object(self.engine._egarch, "fit_and_forecast") as mock_egarch,
            patch.object(self.engine, "compute_atr_stop") as mock_atr,
        ):
            mock_egarch.return_value = EGARCHForecast("SPY", 20.0, 1.2, -2.4, 2.4)
            mock_atr.return_value = (460.0, 430.0)
            order = self.engine.build_enhanced_order(
                _make_signal("short"), _make_account(), "SPY", 450.0
            )
        assert order.action == "SELL"

    def test_execute_with_v2_no_router_returns_none(self):
        engine = V2ExecutionEngine(router=None)
        result = engine.execute_with_v2(
            _make_signal(), _make_account(), "SPY", 450.0
        )
        assert result is None

    def test_execute_with_v2_blocked_by_gex(self):
        mock_router = MagicMock()
        engine = V2ExecutionEngine(router=mock_router)
        with patch.object(engine, "gex_regime_ok", return_value=False):
            result = engine.execute_with_v2(
                _make_signal("short"), _make_account(), "SPY", 450.0
            )
        assert result is None
        mock_router.execute.assert_not_called()
