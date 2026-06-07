"""Tests for trading engine + strategies (Day 8) — end-to-end signal → strategy → router → paper."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker_client.order_manager import OrderManager
from broker_client.order_router import OrderRouter
from broker_client.risk_manager import RiskManager
from broker_client.strategies import load_enabled_strategies
from broker_client.strategies.equity_long_short import EquityLongShort
from broker_client.strategies.momentum_breakout import MomentumBreakout
from broker_client.trading_engine import TradingEngine
from broker_core.base_broker import Account, Order, OrderResult
from core.exceptions import RiskError
from signals.signal_schema import Signal


def _sig(direction="long", confidence=70, source="macro", ticker="AAPL") -> Signal:
    return Signal(
        direction=direction,
        confidence=confidence,
        source=source,
        timestamp=datetime.now(tz=timezone.utc),
        metadata={"ticker": ticker},
    )


def _account(equity=50000.0, buying_power=25000.0) -> Account:
    return Account(account_id="paper_main", cash=equity, equity=equity, buying_power=buying_power)


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT, ticker TEXT, strategy TEXT,
                position_type TEXT, action TEXT, qty INTEGER,
                fill_price REAL, commission REAL, signal_id INTEGER,
                order_id TEXT, env TEXT, timestamp TEXT
            );
        """)
    return db_path


@pytest.fixture
def order_manager(db):
    return OrderManager(db_path=db)


@pytest.fixture
def mock_router():
    router = MagicMock(spec=OrderRouter)
    router.execute.return_value = OrderResult(order_id="TEST-001", status="filled", fill_price=150.0)
    return router


class TestEquityLongShort:
    def test_enters_on_bullish_signal(self):
        strat = EquityLongShort()
        acct = _account()
        signal = _sig(direction="long", confidence=70, source="macro")
        assert strat.should_enter(signal, acct) is True

    def test_no_entry_on_low_confidence(self):
        strat = EquityLongShort()
        acct = _account()
        signal = _sig(direction="long", confidence=30, source="macro")
        assert strat.should_enter(signal, acct) is False

    def test_no_entry_on_neutral(self):
        strat = EquityLongShort()
        acct = _account()
        signal = _sig(direction="neutral", confidence=80, source="macro")
        assert strat.should_enter(signal, acct) is False

    def test_no_entry_on_wrong_source(self):
        strat = EquityLongShort()
        acct = _account()
        signal = _sig(direction="long", confidence=80, source="technical")
        assert strat.should_enter(signal, acct) is False

    def test_build_order_returns_buy_on_long(self):
        strat = EquityLongShort()
        signal = _sig(direction="long", source="macro")
        order = strat.build_order(signal, _account())
        assert order.action == "BUY"
        assert order.qty >= 1

    def test_build_order_returns_sell_on_short(self):
        strat = EquityLongShort()
        signal = _sig(direction="short", source="macro")
        order = strat.build_order(signal, _account())
        assert order.action == "SELL"


class TestMomentumBreakout:
    def test_enters_on_technical_long(self):
        strat = MomentumBreakout()
        acct = _account()
        signal = _sig(direction="long", confidence=70, source="technical", ticker="NVDA")
        assert strat.should_enter(signal, acct) is True

    def test_no_entry_on_macro_source(self):
        strat = MomentumBreakout()
        signal = _sig(direction="long", confidence=80, source="macro")
        assert strat.should_enter(signal, _account()) is False

    def test_no_entry_on_bearish(self):
        strat = MomentumBreakout()
        signal = _sig(direction="short", confidence=80, source="technical")
        assert strat.should_enter(signal, _account()) is False


class TestRiskManager:
    def test_approve_valid_order(self):
        risk = RiskManager()
        acct = _account(equity=100000.0, buying_power=50000.0)
        order = Order(ticker="AAPL", action="BUY", qty=1)  # tiny order
        assert risk.approve(order, acct) is True

    def test_approve_oversized_order_rejected(self):
        risk = RiskManager()
        acct = _account(equity=10000.0, buying_power=5000.0)
        order = Order(ticker="AAPL", action="BUY", qty=1000)  # 100k > 5% of 10k
        assert risk.approve(order, acct) is False

    def test_daily_loss_limit_halts_trading(self):
        risk = RiskManager()
        acct = _account(equity=100000.0)
        with pytest.raises(RiskError):
            risk.record_pnl(-3000.0, acct)  # -3% exceeds 2% limit
        assert risk.is_halted is True

    def test_halted_refuses_all_orders(self):
        risk = RiskManager()
        risk._halted = True
        order = Order(ticker="AAPL", action="BUY", qty=1)
        assert risk.approve(order, _account()) is False

    def test_reset_daily_clears_halt(self):
        risk = RiskManager()
        risk._halted = True
        risk._daily_pnl = -5000.0
        risk.reset_daily()
        assert not risk.is_halted
        assert risk.daily_pnl == 0.0

    def test_kelly_size(self):
        risk = RiskManager()
        acct = _account(equity=100000.0)
        size = risk.kelly_size(acct, win_rate=0.6, avg_win=150.0, avg_loss=100.0)
        assert size > 0
        assert size <= acct.equity * 0.05  # ≤ MAX_POSITION_PCT


class TestTradingEngineEndToEnd:
    def test_signal_flows_to_router(self, mock_router, order_manager):
        risk = RiskManager()
        # Large account so the risk position-size check passes
        acct = _account(equity=10_000_000.0, buying_power=5_000_000.0)
        strategies = load_enabled_strategies(["equity_long_short"])
        engine = TradingEngine(
            router=mock_router,
            risk_manager=risk,
            order_manager=order_manager,
            account=acct,
            strategies=strategies,
        )
        signal = _sig(direction="long", confidence=70, source="macro")
        engine.on_signal(signal)
        mock_router.execute.assert_called_once()

    def test_halted_engine_ignores_signal(self, mock_router, order_manager):
        risk = RiskManager()
        risk._halted = True
        engine = TradingEngine(
            router=mock_router,
            risk_manager=risk,
            order_manager=order_manager,
            account=_account(),
        )
        engine.on_signal(_sig())
        mock_router.execute.assert_not_called()

    def test_strategy_registry_loads(self):
        strategies = load_enabled_strategies(["equity_long_short", "momentum_breakout"])
        assert len(strategies) == 2
        names = [s.strategy_name for s in strategies]
        assert "equity_long_short" in names
        assert "momentum_breakout" in names

    def test_unknown_strategy_skipped(self):
        strategies = load_enabled_strategies(["nonexistent_strategy"])
        assert len(strategies) == 0
