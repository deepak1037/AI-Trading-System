"""Tests for paper_trading/paper_account.py."""

from __future__ import annotations


import pytest

from broker_core.base_broker import Order, OrderPreview
from paper_trading.paper_account import PaperAccount


@pytest.fixture
def account(tmp_path):
    return PaperAccount("test_account", accounts_dir=str(tmp_path))


class TestPaperAccountInit:
    def test_creates_json_file(self, tmp_path):
        PaperAccount("test_init", accounts_dir=str(tmp_path))
        assert (tmp_path / "test_init.json").exists()

    def test_initial_state_is_empty(self, account):
        state = account.get_state()
        assert state.cash == 0.0
        assert state.equity == 0.0
        assert len(state.open_positions) == 0


class TestPaperAccountBalance:
    def test_initialize_balance(self, account):
        account.initialize_balance(50000.0, "Test start")
        state = account.get_state()
        assert state.cash == 50000.0
        assert state.initial_cash == 50000.0

    def test_balance_history_logged(self, account):
        account.initialize_balance(50000.0)
        state = account.get_state()
        assert len(state.balance_history) == 1
        assert state.balance_history[0].action == "init"

    def test_manual_adjustment_positive(self, account):
        account.initialize_balance(10000.0)
        account.manual_adjustment(5000.0, "test add")
        state = account.get_state()
        assert state.cash == 15000.0

    def test_manual_adjustment_negative(self, account):
        account.initialize_balance(10000.0)
        account.manual_adjustment(-2000.0, "test withdrawal")
        state = account.get_state()
        assert state.cash == 8000.0


class TestPaperAccountFills:
    def test_buy_reduces_cash(self, account):
        account.initialize_balance(50000.0)
        order = Order(ticker="AAPL", action="BUY", qty=10, strategy_name="test")
        account.apply_fill(order, fill_price=150.0)
        state = account.get_state()
        assert state.cash == 50000.0 - 1500.0
        assert len(state.open_positions) == 1

    def test_sell_closes_position(self, account):
        account.initialize_balance(50000.0)
        buy_order = Order(ticker="AAPL", action="BUY", qty=10, strategy_name="test")
        account.apply_fill(buy_order, fill_price=150.0)
        sell_order = Order(ticker="AAPL", action="SELL", qty=10, strategy_name="test")
        account.apply_fill(sell_order, fill_price=160.0)
        state = account.get_state()
        assert len(state.open_positions) == 0
        assert len(state.closed_positions) == 1
        pnl = state.closed_positions[0]["realized_pnl"]
        assert abs(pnl - 100.0) < 0.01  # (160-150)*10

    def test_simulate_10_trades(self, account):
        account.initialize_balance(50000.0)
        prices = [100, 105, 98, 112, 108, 115, 103, 120, 110, 125]
        exits = [105, 102, 105, 109, 112, 112, 108, 118, 115, 130]
        for entry, exit_ in zip(prices, exits):
            buy = Order(ticker="TEST", action="BUY", qty=5, strategy_name="test")
            account.apply_fill(buy, float(entry))
            sell = Order(ticker="TEST", action="SELL", qty=5, strategy_name="test")
            account.apply_fill(sell, float(exit_))
        state = account.get_state()
        assert len(state.open_positions) == 0
        assert len(state.closed_positions) == 10

    def test_apply_preview_valid(self, account):
        account.initialize_balance(50000.0)
        order = Order(ticker="SPY", action="BUY", qty=10, strategy_name="test")
        preview = OrderPreview(estimated_cost=5000.0, is_valid=True, fees=0.0)
        result = account.apply_preview(order, preview)
        assert result.status == "filled"

    def test_apply_preview_rejected(self, account):
        order = Order(ticker="SPY", action="BUY", qty=10, strategy_name="test")
        preview = OrderPreview(is_valid=False, rejection_reason="Insufficient funds")
        result = account.apply_preview(order, preview)
        assert result.status == "rejected"


class TestPaperAccountReset:
    def test_reset_clears_positions(self, account):
        account.initialize_balance(50000.0)
        buy = Order(ticker="AAPL", action="BUY", qty=5, strategy_name="test")
        account.apply_fill(buy, 150.0)
        account.reset(confirm=True)
        state = account.get_state()
        assert len(state.open_positions) == 0
        assert state.cash == 0.0

    def test_reset_with_confirm_false_does_nothing(self, account):
        account.initialize_balance(50000.0)
        account.reset(confirm=False)
        state = account.get_state()
        assert state.cash == 50000.0
