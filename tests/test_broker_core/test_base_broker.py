"""Tests for broker_core/base_broker.py pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from broker_core.base_broker import (
    Account, Order, OrderResult, OrderPreview,
    Position, OptionsChain, OptionsContract,
)


class TestOrderModel:
    def test_valid_order(self):
        o = Order(ticker="AAPL", action="BUY", qty=10)
        assert o.ticker == "AAPL"
        assert o.qty == 10
        assert o.order_type == "market"

    def test_qty_must_be_positive(self):
        with pytest.raises(ValidationError):
            Order(ticker="AAPL", action="BUY", qty=0)

    def test_limit_order_with_price(self):
        o = Order(ticker="AAPL", action="BUY", qty=5, order_type="limit", limit_price=150.0)
        assert o.limit_price == 150.0

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            Order(ticker="AAPL", action="HOLD", qty=10)


class TestOrderResult:
    def test_dry_run_result(self):
        r = OrderResult(order_id="DRY_RUN", status="dry_run", fill_price=0.0)
        assert r.status == "dry_run"

    def test_filled_result(self):
        from datetime import datetime, timezone
        r = OrderResult(
            order_id="abc123",
            fill_price=150.25,
            commission=0.0,
            status="filled",
            filled_at=datetime.now(tz=timezone.utc),
        )
        assert r.fill_price == 150.25


class TestOrderPreview:
    def test_valid_preview(self):
        p = OrderPreview(estimated_cost=1500.0, is_valid=True)
        assert p.is_valid is True
        assert p.rejection_reason is None

    def test_rejected_preview(self):
        p = OrderPreview(is_valid=False, rejection_reason="Insufficient funds")
        assert p.is_valid is False


class TestPosition:
    def test_basic_position(self):
        pos = Position(
            ticker="SPY",
            qty=100,
            avg_cost=450.0,
            strategy_name="equity_long_short",
        )
        assert pos.ticker == "SPY"
        assert pos.unrealized_pnl == 0.0


class TestAccount:
    def test_account(self):
        a = Account(account_id="paper_main", cash=50000.0, equity=55000.0)
        assert a.cash == 50000.0


class TestOptionsChain:
    def test_empty_chain(self):
        chain = OptionsChain(ticker="AAPL", expiry="2024-01-19")
        assert chain.calls == []
        assert chain.puts == []

    def test_chain_with_contracts(self):
        call = OptionsContract(
            symbol="AAPL240119C00150000",
            expiry="2024-01-19",
            strike=150.0,
            option_type="call",
            bid=5.0,
            ask=5.50,
        )
        chain = OptionsChain(ticker="AAPL", expiry="2024-01-19", calls=[call])
        assert len(chain.calls) == 1
        assert chain.calls[0].strike == 150.0
