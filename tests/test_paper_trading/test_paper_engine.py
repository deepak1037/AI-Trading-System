"""Tests for paper_trading/paper_engine.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from broker_core.base_broker import OptionsOrder, Order
from paper_trading.paper_engine import PaperTradingEngine


@pytest.fixture
def engine():
    return PaperTradingEngine()


_PRICE_DATA = {
    "open": 150.0,
    "high": 155.0,
    "low": 148.0,
    "close": 152.0,
    "vwap": 151.0,
}


class TestPaperEngineEquity:
    def test_next_open_fill(self, engine):
        with patch("config.settings.settings.PAPER_FILL_METHOD", "next_open"):
            order = Order(ticker="AAPL", action="BUY", qty=10)
            result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        assert result.status == "filled"
        # next_open uses 'open' price + slippage
        assert result.fill_price > _PRICE_DATA["open"] * 0.99

    def test_vwap_fill(self, engine):
        with patch("config.settings.settings.PAPER_FILL_METHOD", "vwap"):
            order = Order(ticker="AAPL", action="BUY", qty=10)
            result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        assert abs(result.fill_price - _PRICE_DATA["vwap"] * 1.001) < 0.05  # +slippage

    def test_worst_case_buy_uses_high(self, engine):
        with patch("config.settings.settings.PAPER_FILL_METHOD", "worst_case"):
            order = Order(ticker="AAPL", action="BUY", qty=5)
            result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        # Worst case buy = high + slippage
        assert result.fill_price >= _PRICE_DATA["high"]

    def test_worst_case_sell_uses_low(self, engine):
        with patch("config.settings.settings.PAPER_FILL_METHOD", "worst_case"):
            order = Order(ticker="AAPL", action="SELL", qty=5)
            result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        # Worst case sell = low - slippage
        assert result.fill_price <= _PRICE_DATA["low"]

    def test_fill_has_order_id(self, engine):
        order = Order(ticker="SPY", action="BUY", qty=100)
        result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        assert "SPY" in result.order_id

    def test_commission_applied(self, engine):
        with patch("config.settings.settings.PAPER_COMMISSION_PER_SHARE", 0.01):
            order = Order(ticker="AAPL", action="BUY", qty=100)
            result = engine.simulate_fill(order, price_data=_PRICE_DATA)
        assert result.commission == pytest.approx(1.00, abs=0.001)


class TestPaperEngineOptions:
    def test_options_fill_with_mid_price(self, engine):
        order = OptionsOrder(
            ticker="AAPL",
            action="BUY_TO_OPEN",
            contract="AAPL240119C00150000",
            qty=1,
            limit_price=5.0,
        )
        result = engine.simulate_options_fill(order, mid_price=5.0)
        assert result.status == "filled"
        assert result.fill_price > 0

    def test_options_slippage_applied(self, engine):
        order = OptionsOrder(
            ticker="AAPL",
            action="BUY_TO_OPEN",
            contract="AAPL240119C00150000",
            qty=2,
            limit_price=5.0,
        )
        with patch("config.settings.settings.PAPER_OPTIONS_SLIPPAGE", 0.5):
            result = engine.simulate_options_fill(order, mid_price=5.0)
        # 50% slippage on buy = mid * 1.5
        assert result.fill_price > 5.0

    def test_options_commission_per_contract(self, engine):
        order = OptionsOrder(
            ticker="AAPL",
            action="SELL_TO_OPEN",
            contract="AAPL240119C00150000",
            qty=3,
            limit_price=5.0,
        )
        with patch("config.settings.settings.PAPER_OPTIONS_COMMISSION", 0.65):
            result = engine.simulate_options_fill(order, mid_price=5.0)
        assert result.commission == pytest.approx(1.95, abs=0.001)
