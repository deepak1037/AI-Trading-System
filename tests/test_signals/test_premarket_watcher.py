"""Tests for signals/premarket_watcher.py."""

from __future__ import annotations

import pytest

from signals.premarket_watcher import PremarketWatcher


@pytest.fixture
def watcher():
    return PremarketWatcher()


class TestPremarketWatcher:
    def test_neutral_on_small_move(self, watcher):
        signal = watcher.check(nq_change=0.001, es_change=0.001, btc_change=0.002)
        assert signal.source == "premarket"
        assert signal.direction == "neutral"

    def test_short_signal_on_nq_drop(self, watcher):
        # NQ down -1.2% (> 0.8% threshold)
        signal = watcher.check(nq_change=-0.012, es_change=-0.008, btc_change=-0.01)
        assert signal.direction in ("short", "strong_short")

    def test_long_signal_on_nq_rally(self, watcher):
        # NQ up +1.5%
        signal = watcher.check(nq_change=0.015, es_change=0.012, btc_change=0.02)
        assert signal.direction in ("long", "strong_long")

    def test_strong_short_on_severe_drop(self, watcher):
        signal = watcher.check(nq_change=-0.030, es_change=-0.025, btc_change=-0.05)
        assert signal.direction == "strong_short"
        assert signal.confidence >= 60

    def test_metadata_contains_all_fields(self, watcher):
        signal = watcher.check(nq_change=-0.01, es_change=-0.008, btc_change=0.0)
        assert "nq_change" in signal.metadata
        assert "es_change" in signal.metadata
        assert "btc_change" in signal.metadata
        assert "composite_pct" in signal.metadata
        assert "threshold" in signal.metadata

    def test_confidence_in_range(self, watcher):
        for nq in [-0.05, -0.01, 0.0, 0.01, 0.05]:
            signal = watcher.check(nq_change=nq, es_change=0.0, btc_change=0.0)
            assert 0 <= signal.confidence <= 100

    def test_btc_risk_proxy_contributes(self, watcher):
        # Modest NQ + large BTC rally
        signal_with_btc = watcher.check(nq_change=0.005, es_change=0.004, btc_change=0.10)
        signal_no_btc = watcher.check(nq_change=0.005, es_change=0.004, btc_change=0.0)
        # BTC contribution should push composite higher
        assert signal_with_btc.metadata["composite_pct"] > signal_no_btc.metadata["composite_pct"]

    def test_live_fetch_uses_zero_on_api_failure(self, watcher, mocker):
        from core.exceptions import DataError
        mocker.patch.object(watcher, "_fetch_futures_change", side_effect=DataError("down"))
        mocker.patch.object(watcher, "_fetch_btc_24h_change", return_value=0.0)
        signal = watcher.check()
        assert signal.source == "premarket"
