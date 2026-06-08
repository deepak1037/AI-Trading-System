"""Tests for signals/yield_monitor.py."""

from __future__ import annotations


import pytest

from core.exceptions import SignalError
from signals.yield_monitor import YieldMonitor


@pytest.fixture
def monitor():
    return YieldMonitor()


class TestYieldMonitorBaseline:
    def test_set_baseline_stores_value(self, monitor):
        baseline = monitor.set_baseline(yield_pct=4.25)
        assert monitor.baseline == 4.25
        assert baseline == 4.25

    def test_get_delta_raises_without_baseline(self, monitor):
        with pytest.raises(SignalError):
            monitor.get_delta(4.30)

    def test_get_delta_positive(self, monitor):
        monitor.set_baseline(4.00)
        delta = monitor.get_delta(4.10)
        assert abs(delta - 0.10) < 0.0001

    def test_get_delta_negative(self, monitor):
        monitor.set_baseline(4.00)
        delta = monitor.get_delta(3.90)
        assert abs(delta + 0.10) < 0.0001


class TestYieldMonitorSignal:
    def test_no_signal_below_threshold(self, monitor):
        monitor.set_baseline(4.00)
        signal = monitor.check_delta(4.02)  # 2 bps — below default 5 bps
        assert signal is None

    def test_signal_above_threshold(self, monitor):
        monitor.set_baseline(4.00)
        signal = monitor.check_delta(4.06)  # 6 bps — above default 5 bps
        assert signal is not None
        assert signal.source == "yield"
        assert signal.direction in ("short", "strong_short")  # rising yields = bearish

    def test_falling_yields_bullish(self, monitor):
        monitor.set_baseline(4.00)
        signal = monitor.check_delta(3.93)  # 7 bps drop
        assert signal is not None
        assert signal.direction in ("long", "strong_long")

    def test_large_spike_strong_direction(self, monitor):
        monitor.set_baseline(4.00)
        signal = monitor.check_delta(4.25)  # 25 bps spike
        assert signal is not None
        assert signal.direction == "strong_short"
        assert signal.confidence >= 75

    def test_metadata_contains_delta(self, monitor):
        monitor.set_baseline(4.00)
        signal = monitor.check_delta(4.08)
        assert signal is not None
        assert abs(signal.metadata["yield_delta"] - 0.08) < 0.001
        assert signal.metadata["yield_baseline"] == 4.00

    def test_confidence_range(self, monitor):
        monitor.set_baseline(4.00)
        for current in [4.06, 4.10, 4.20, 4.50]:
            sig = monitor.check_delta(current)
            if sig:
                assert 0 <= sig.confidence <= 100

    def test_exact_threshold_no_signal(self, monitor):
        from config.settings import settings
        monitor.set_baseline(4.00)
        # Exactly at threshold — should be None (< threshold)
        signal = monitor.check_delta(4.00 + settings.YIELD_DELTA_THRESHOLD - 0.001)
        assert signal is None


class TestYieldRead:
    """read() — continuous reading for the fusion tick (the fix)."""

    def test_read_autosets_baseline(self, monitor, mocker):
        mocker.patch.object(monitor, "_fetch_current_yield", side_effect=[4.00, 4.00])
        sig = monitor.read()
        assert sig is not None
        assert sig.source == "yield"
        assert monitor.baseline == 4.00  # baseline set automatically

    def test_read_neutral_below_threshold(self, monitor, mocker):
        mocker.patch.object(monitor, "_fetch_current_yield", side_effect=[4.00, 4.02])  # 2 bps
        sig = monitor.read()
        assert sig is not None
        assert sig.direction == "neutral"  # still returns a signal (continuous)

    def test_read_directional_above_threshold(self, monitor, mocker):
        mocker.patch.object(monitor, "_fetch_current_yield", side_effect=[4.00, 4.10])  # +10 bps
        sig = monitor.read()
        assert sig.direction in ("short", "strong_short")  # rising yields = bearish
        assert sig.confidence >= 55

    def test_read_returns_none_when_no_data(self, monitor, mocker):
        from core.exceptions import DataError
        mocker.patch.object(monitor, "_fetch_current_yield", side_effect=DataError("no intraday"))
        mocker.patch.object(monitor, "_fetch_fred_daily_yield", side_effect=DataError("no fred"))
        assert monitor.read() is None

    def test_signal_from_delta_neutral_and_directional(self, monitor):
        monitor.set_baseline(4.00)
        assert monitor._signal_from_delta(0.01).direction == "neutral"   # 1 bp
        assert monitor._signal_from_delta(0.10).direction in ("short", "strong_short")
        assert monitor._signal_from_delta(-0.10).direction in ("long", "strong_long")
