"""Tests for broker_client/position_watcher.py (Day 9)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker_client.order_router import OrderRouter
from broker_client.position_manager import PositionManager
from broker_client.position_watcher import PositionWatcher
from broker_core.base_broker import Order, OrderResult, Position
from signals.signal_schema import MarketSnapshot


def _snap(ticker="AAPL", price=150.0) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=datetime.now(tz=timezone.utc),
        ticker=ticker,
        price=price,
        volume=100_000,
    )


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                position_type TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                exit_reason TEXT,
                realized_pnl REAL,
                is_open INTEGER DEFAULT 1
            );
        """)
    return path


@pytest.fixture
def pm(db):
    return PositionManager(db_path=db)


@pytest.fixture
def mock_router():
    r = MagicMock(spec=OrderRouter)
    r.execute.return_value = OrderResult(order_id="EXIT-001", status="filled", fill_price=155.0)
    return r


@pytest.fixture
def watcher(pm, mock_router):
    return PositionWatcher(broker=None, router=mock_router, position_manager=pm)


class TestPositionWatcherStartup:
    def test_startup_load_empty_db(self, watcher):
        watcher.startup_load()
        assert watcher.positions == {}

    def test_startup_load_with_db_positions(self, watcher, pm):
        pm.open_position("paper_main", "AAPL", "equity_long_short", "equity_long", 10, 150.0)
        watcher.startup_load()
        assert "AAPL" in watcher.positions

    def test_startup_reconciles_broker_truth(self, watcher, pm):
        """Broker positions supersede DB when broker is available."""
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = [
            Position(ticker="SPY", qty=100, avg_cost=450.0, strategy_name="equity_long_short")
        ]
        watcher._broker = mock_broker
        watcher.startup_load()
        assert "SPY" in watcher.positions
        watcher._broker = None


class TestPositionWatcherAutoRegister:
    def test_register_on_buy_fill(self, watcher):
        order = Order(ticker="AAPL", action="BUY", qty=10, strategy_name="equity_long_short")
        result = OrderResult(order_id="001", status="filled", fill_price=150.0)
        watcher.register_if_filled(order, result)
        assert "AAPL" in watcher.positions
        assert watcher.positions["AAPL"]["qty"] == 10

    def test_no_register_on_pending(self, watcher):
        order = Order(ticker="AAPL", action="BUY", qty=10)
        result = OrderResult(order_id="001", status="pending", fill_price=0.0)
        watcher.register_if_filled(order, result)
        assert "AAPL" not in watcher.positions

    def test_deregister_on_sell(self, watcher):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "strategy_name": "equity_long_short", "stop_loss": None, "take_profit": None,
        })
        order = Order(ticker="AAPL", action="SELL", qty=10)
        result = OrderResult(order_id="002", status="filled", fill_price=155.0)
        watcher.register_if_filled(order, result)
        assert "AAPL" not in watcher.positions


class TestPositionWatcherStopLoss:
    def test_stop_loss_triggers_close(self, watcher, mock_router):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "current_price": 145.0, "strategy_name": "equity_long_short",
            "stop_loss": 145.0, "take_profit": None,
            "position_type": "equity_long",
        })
        snap = _snap("AAPL", 144.0)  # below stop
        watcher.on_market_tick(snap)
        mock_router.execute.assert_called_once()
        assert "AAPL" not in watcher.positions

    def test_take_profit_triggers_close(self, watcher, mock_router):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "current_price": 150.0, "strategy_name": "equity_long_short",
            "stop_loss": None, "take_profit": 165.0,
            "position_type": "equity_long",
        })
        snap = _snap("AAPL", 166.0)  # above take-profit
        watcher.on_market_tick(snap)
        mock_router.execute.assert_called_once()

    def test_no_close_within_range(self, watcher, mock_router):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "current_price": 152.0, "strategy_name": "equity_long_short",
            "stop_loss": 140.0, "take_profit": 170.0,
            "position_type": "equity_long",
        })
        snap = _snap("AAPL", 155.0)
        watcher.on_market_tick(snap)
        mock_router.execute.assert_not_called()


class TestPositionWatcherRegimeChange:
    def test_regime_close_on_strong_short(self, watcher, mock_router):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "current_price": 150.0, "strategy_name": "equity_long_short",
            "stop_loss": None, "take_profit": None,
        })
        watcher.on_regime_change("strong_short")
        mock_router.execute.assert_called_once()
        assert "AAPL" not in watcher.positions

    def test_regime_change_unknown_regime(self, watcher, mock_router):
        watcher.register({
            "ticker": "AAPL", "qty": 10, "entry_price": 150.0,
            "current_price": 150.0, "strategy_name": "equity_long_short",
            "stop_loss": None, "take_profit": None,
        })
        watcher.on_regime_change("neutral")  # not in triggers
        mock_router.execute.assert_not_called()
        assert "AAPL" in watcher.positions
