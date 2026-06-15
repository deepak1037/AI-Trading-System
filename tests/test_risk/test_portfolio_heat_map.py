"""Tests for broker_client/risk/portfolio_heat_map.py."""

from __future__ import annotations

import pytest

from broker_client.risk.portfolio_heat_map import (
    CORRELATION_CLUSTERS,
    SECTOR_MAP,
    PortfolioHeatMap,
)

# ── shared fixtures ───────────────────────────────────────────────────────────

_PUT_POSITION = {
    "ticker": "NVDA", "option_type": "put", "position_type": "options_short",
    "strike": 700.0, "expiry": "2025-12-19", "qty": 1,
    "entry_price": 10.0, "delta": -0.3,
}
_CALL_POSITION = {
    "ticker": "AAPL", "option_type": "call", "position_type": "options_long",
    "strike": 200.0, "expiry": "2025-11-21", "qty": 2,
    "entry_price": 5.0, "delta": 0.5,
}
_TECH_POSITION = {
    "ticker": "MSFT", "option_type": "put", "position_type": "options_short",
    "strike": 400.0, "expiry": "2025-12-19", "qty": 1,
    "entry_price": 8.0, "delta": -0.25,
}

_POSITIONS = [_PUT_POSITION, _CALL_POSITION, _TECH_POSITION]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_sector_concentration_calculation():
    hm = PortfolioHeatMap()
    report = hm.generate(_POSITIONS)
    # NVDA is Semiconductors, AAPL and MSFT are Technology
    assert "Semiconductors" in report.sector_exposure
    assert "Technology" in report.sector_exposure
    total = sum(s.exposure_pct for s in report.sector_exposure.values())
    assert abs(total - 100.0) < 1e-3


def test_sector_warning_above_40pct():
    # 10 NVDA positions → Semiconductors >> 40%
    many = [dict(_PUT_POSITION) for _ in range(10)]
    hm = PortfolioHeatMap()
    report = hm.generate(many)
    assert report.sector_warning is True
    assert report.max_sector_pct > 40.0


def test_no_sector_warning_balanced():
    # Mix sectors to stay under the threshold
    tickers = ["NVDA", "JNJ", "VLO", "AAPL", "CAT"]
    positions = [
        {"ticker": t, "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0, "delta": -0.3}
        for t in tickers
    ]
    hm = PortfolioHeatMap()
    report = hm.generate(positions)
    # All equal-weight at 20% each → none > 40%
    assert report.sector_warning is False


def test_net_delta_calculation_short_puts():
    """Short put has positive delta (bullish)."""
    pos = {
        "ticker": "NVDA", "option_type": "put", "position_type": "options_short",
        "qty": 1, "entry_price": 10.0, "delta": -0.3,
    }
    hm = PortfolioHeatMap()
    report = hm.generate([pos])
    # Short puts: sign reversal → effective delta = +0.3 × 100 = +30
    assert report.net_delta == pytest.approx(30.0)
    assert report.delta_direction == "neutral"  # < 50


def test_net_delta_calculation_long_calls():
    pos = {
        "ticker": "AAPL", "option_type": "call", "position_type": "options_long",
        "qty": 2, "entry_price": 5.0, "delta": 0.5,
    }
    hm = PortfolioHeatMap()
    report = hm.generate([pos])
    # Long call: +0.5 × 2 × 100 = +100
    assert report.net_delta == pytest.approx(100.0)
    assert report.delta_direction == "bullish"


def test_same_expiry_concentration(monkeypatch):
    from config.settings import settings
    from datetime import date, timedelta
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 3)
    # Use a future Friday (same week)
    future_friday = (date.today() + timedelta(days=14)).isoformat()
    positions = [
        {"ticker": t, "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0, "expiry": future_friday}
        for t in ["NVDA", "AAPL", "MSFT"]
    ]
    hm = PortfolioHeatMap()
    report = hm.generate(positions)
    assert report.same_expiry_warning is True


def test_no_expiry_warning_spread(monkeypatch):
    from config.settings import settings
    from datetime import date, timedelta
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 5)
    positions = [
        {"ticker": "NVDA", "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0,
         "expiry": (date.today() + timedelta(days=14)).isoformat()},
        {"ticker": "AAPL", "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0,
         "expiry": (date.today() + timedelta(days=45)).isoformat()},
    ]
    hm = PortfolioHeatMap()
    report = hm.generate(positions)
    assert report.same_expiry_warning is False


def test_correlated_cluster_detection():
    positions = [
        {"ticker": t, "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0}
        for t in ["NVDA", "AMD", "INTC"]  # all Semiconductors cluster
    ]
    hm = PortfolioHeatMap()
    report = hm.generate(positions)
    cluster_names = [c.cluster_name for c in report.correlated_clusters]
    assert "Semiconductors" in cluster_names


def test_no_cluster_when_single_per_group():
    positions = [
        {"ticker": "NVDA", "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0},
        {"ticker": "JNJ", "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 5.0},
    ]
    hm = PortfolioHeatMap()
    report = hm.generate(positions)
    # NVDA alone in Semiconductors cluster (JNJ not there) → no cluster
    assert len(report.correlated_clusters) == 0


def test_cash_buffer_calculation():
    hm = PortfolioHeatMap()
    report = hm.generate([])
    # Without paper account, falls back to settings cash_buffer_pct
    from config.settings import settings
    expected = settings.cash_buffer_pct
    assert report.cash_buffer_pct == pytest.approx(float(expected))


def test_empty_positions():
    hm = PortfolioHeatMap()
    report = hm.generate([])
    assert report.total_positions == 0
    assert report.total_exposure == 0.0
    assert report.net_delta == pytest.approx(0.0)
    assert report.correlated_clusters == []
