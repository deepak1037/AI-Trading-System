"""Tests for broker_client/order_router.py — tests all three routing paths."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from broker_client.order_router import OrderRouter
from broker_core.base_broker import Order, OrderPreview, OrderResult


def _make_order(**kwargs) -> Order:
    defaults = {"ticker": "AAPL", "action": "BUY", "qty": 10}
    defaults.update(kwargs)
    return Order(**defaults)


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.place_order.return_value = OrderResult(order_id="LIVE-001", status="pending", fill_price=150.0)
    broker.preview_order.return_value = OrderPreview(estimated_cost=1500.0, is_valid=True)
    return broker


@pytest.fixture
def mock_paper_engine():
    engine = MagicMock()
    engine.simulate_fill.return_value = OrderResult(order_id="PAPER-001", status="filled", fill_price=149.50)
    return engine


class TestOrderRouterDevelopment:
    def test_development_mode_returns_dry_run(self, mock_broker):
        with patch("config.settings.settings.ENV", "development"):
            router = OrderRouter(broker=mock_broker)
            result = router.execute(_make_order())
        assert result.status == "dry_run"
        assert result.order_id == "DRY_RUN"
        mock_broker.place_order.assert_not_called()

    def test_development_never_calls_broker(self, mock_broker):
        with patch("config.settings.settings.ENV", "development"):
            router = OrderRouter(broker=mock_broker)
            router.execute(_make_order())
        mock_broker.place_order.assert_not_called()
        mock_broker.preview_order.assert_not_called()


class TestOrderRouterBacktest:
    def test_backtest_calls_paper_engine(self, mock_broker, mock_paper_engine):
        with patch("config.settings.settings.ENV", "backtest"):
            router = OrderRouter(broker=mock_broker, paper_engine=mock_paper_engine)
            result = router.execute(_make_order())
        assert result.order_id == "PAPER-001"
        mock_paper_engine.simulate_fill.assert_called_once()
        mock_broker.place_order.assert_not_called()

    def test_backtest_without_engine_returns_dry_run(self, mock_broker):
        with patch("config.settings.settings.ENV", "backtest"):
            router = OrderRouter(broker=mock_broker)
            result = router.execute(_make_order())
        assert result.status == "dry_run"


class TestOrderRouterPaper:
    def test_paper_calls_broker_preview(self, mock_broker):
        with patch("config.settings.settings.ENV", "paper"):
            router = OrderRouter(broker=mock_broker)
            router.execute(_make_order())
        mock_broker.preview_order.assert_called_once()
        mock_broker.place_order.assert_not_called()

    def test_paper_with_paper_account(self, mock_broker):
        mock_paper_acct = MagicMock()
        mock_paper_acct.apply_preview.return_value = OrderResult(
            order_id="PA-001", status="filled", fill_price=150.0
        )
        with patch("config.settings.settings.ENV", "paper"):
            router = OrderRouter(broker=mock_broker, paper_account=mock_paper_acct)
            result = router.execute(_make_order())
        mock_paper_acct.apply_preview.assert_called_once()
        assert result.order_id == "PA-001"


class TestOrderRouterLive:
    def test_live_requires_triple_gate(self, mock_broker):
        """Without all three gates, order should NOT go live."""
        # ENV=live but DRY_RUN=True → development path
        with (
            patch("config.settings.settings.ENV", "development"),
            patch("config.settings.settings.DRY_RUN", True),
            patch("config.settings.settings.LIVE_TRADING_ENABLED", False),
        ):
            router = OrderRouter(broker=mock_broker)
            result = router.execute(_make_order())
        mock_broker.place_order.assert_not_called()
        assert result.status == "dry_run"

    def test_live_triple_gate_calls_broker(self, mock_broker):
        with (
            patch("config.settings.settings.ENV", "live"),
            patch("config.settings.settings.DRY_RUN", False),
            patch("config.settings.settings.LIVE_TRADING_ENABLED", True),
        ):
            router = OrderRouter(broker=mock_broker)
            result = router.execute(_make_order())
        mock_broker.place_order.assert_called_once()
        assert result.order_id == "LIVE-001"
