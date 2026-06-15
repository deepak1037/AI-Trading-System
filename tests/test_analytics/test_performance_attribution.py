"""Tests for broker_client/analytics/performance_attribution.py."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from broker_client.analytics.performance_attribution import PerformanceAttribution
from data.db import get_connection, init_db


def _seed_trades(db_path: str, trades: list[dict]) -> None:
    today = date.today()
    with get_connection(db_path) as conn:
        for t in trades:
            opened = (today - timedelta(days=t.get("hold_days", 5))).isoformat()
            closed = today.isoformat()
            conn.execute(
                "INSERT INTO positions "
                "(account_id, ticker, strategy, position_type, qty, entry_price, "
                " opened_at, closed_at, realized_pnl, is_open, bucket) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (
                    "paper_main", t["ticker"], t.get("strategy", "msp"),
                    "options_short", 1, t.get("fill_price", 10.0),
                    opened, closed, t["realized_pnl"], t.get("bucket", 1),
                ),
            )


@pytest.fixture
def db_with_trades(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    _seed_trades(db_path, [
        {"ticker": "NVDA", "realized_pnl": 500, "bucket": 1},
        {"ticker": "AAPL", "realized_pnl": 300, "bucket": 1},
        {"ticker": "JNJ", "realized_pnl": -100, "bucket": 2},
        {"ticker": "VLO", "realized_pnl": 200, "bucket": 1},
        {"ticker": "AMD", "realized_pnl": -50, "bucket": 3},
    ])
    return db_path


def test_bucket_pnl_calculation(db_with_trades):
    attr = PerformanceAttribution(db_path=db_with_trades)
    report = attr.generate_report("MTD")
    assert not report.insufficient_data
    b1 = report.bucket_attribution[1]
    assert b1.realized_pnl == pytest.approx(1000.0)  # 500+300+200


def test_win_rate_calculation(db_with_trades):
    attr = PerformanceAttribution(db_path=db_with_trades)
    report = attr.generate_report("MTD")
    # 3 wins (NVDA, AAPL, VLO), 2 losses (JNJ, AMD)
    assert report.win_rate == pytest.approx(60.0)


def test_profit_factor_calculation(db_with_trades):
    attr = PerformanceAttribution(db_path=db_with_trades)
    report = attr.generate_report("MTD")
    gross_profit = 500 + 300 + 200  # = 1000
    gross_loss = 100 + 50           # = 150
    assert report.profit_factor == pytest.approx(gross_profit / gross_loss, rel=1e-3)


def test_best_worst_strategy_detection(db_with_trades):
    attr = PerformanceAttribution(db_path=db_with_trades)
    report = attr.generate_report("MTD")
    assert report.best_trade is not None
    assert report.worst_trade is not None
    assert report.best_trade.realized_pnl >= report.worst_trade.realized_pnl


def test_insufficient_data_when_few_trades(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_TRADES_FOR_ATTRIBUTION", 10)

    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    _seed_trades(db_path, [{"ticker": "NVDA", "realized_pnl": 100, "bucket": 1}])

    attr = PerformanceAttribution(db_path=db_path)
    report = attr.generate_report("MTD")
    assert report.insufficient_data is True
    assert report.total_pnl == 0.0


def test_empty_attribution(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    attr = PerformanceAttribution(db_path=db_path)
    report = attr.generate_report("MTD")
    assert report.insufficient_data is True


def test_period_start_mtd():
    today = date.today()
    start = PerformanceAttribution._period_start("MTD")
    assert start == today.replace(day=1)


def test_period_start_ytd():
    today = date.today()
    start = PerformanceAttribution._period_start("YTD")
    assert start == today.replace(month=1, day=1)
