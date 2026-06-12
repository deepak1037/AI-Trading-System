"""Tests for OrderRouter scale-out / ladder execution (Step 7)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from broker_client.order_router import LadderLeg, OrderRouter
from broker_core.base_broker import OrderResult


@pytest.fixture
def router():
    broker = MagicMock()
    return OrderRouter(broker=broker)


def _pos(**kw):
    base = {
        "ticker": "NVDA", "qty": 10, "strategy": "long_call",
        "option_type": "call", "contract": "NVDA260116C00300000",
        "position_type": "options_long",
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestLadder:
    def test_default_three_tranches(self, router):
        legs = router.execute_ladder(_pos(qty=10))
        assert len(legs) == 3
        assert [leg.kind for leg in legs] == ["close", "deferred", "trailing"]

    def test_contracts_sum_to_total(self, router):
        legs = router.execute_ladder(_pos(qty=10))
        assert sum(leg.contracts for leg in legs) == 10

    def test_contracts_sum_with_rounding(self, router):
        # 7 contracts over [0.3,0.4,0.3] → 2 + 2 + remainder 3 = 7.
        legs = router.execute_ladder(_pos(qty=7))
        assert sum(leg.contracts for leg in legs) == 7

    def test_first_tranche_closes_now(self, router):
        legs = router.execute_ladder(_pos(qty=10))
        first = legs[0]
        assert first.kind == "close"
        # development ENV → dry_run close.
        assert first.status in {"dry_run", "filled"}

    def test_deferred_leg_has_trigger(self, router):
        legs = router.execute_ladder(_pos(qty=10))
        deferred = legs[1]
        assert deferred.kind == "deferred"
        assert deferred.trigger_profit_pct == 70.0
        assert deferred.status == "armed"

    def test_trailing_leg_breakeven(self, router):
        legs = router.execute_ladder(_pos(qty=10))
        trailing = legs[2]
        assert trailing.kind == "trailing"
        assert trailing.stop_at_pct == 0.0

    def test_custom_tranches(self, router):
        legs = router.execute_ladder(_pos(qty=10), tranches=[0.5, 0.5])
        assert len(legs) == 2
        assert sum(leg.contracts for leg in legs) == 10

    def test_tranches_must_sum_to_one(self, router):
        with pytest.raises(ValueError, match="sum to 1.0"):
            router.execute_ladder(_pos(qty=10), tranches=[0.5, 0.3])

    def test_zero_qty_raises(self, router):
        with pytest.raises(ValueError, match="non-positive"):
            router.execute_ladder(_pos(qty=0))

    def test_accepts_dict_position(self, router):
        legs = router.execute_ladder({"ticker": "AAPL", "qty": 6, "option_type": None})
        assert sum(leg.contracts for leg in legs) == 6


class TestClosePosition:
    def test_close_short_option_buys_to_close(self):
        broker = MagicMock()
        broker.place_options_order.return_value = OrderResult(
            order_id="C1", status="filled", fill_price=0.5
        )
        router = OrderRouter(broker=broker)
        # Force the options path through execute_options in development → dry_run,
        # but verify the action chosen for a short option is BUY_TO_CLOSE.
        captured = {}
        router.execute_options = lambda o: captured.update(action=o.action) or OrderResult(
            order_id="X", status="dry_run", fill_price=0.0
        )
        router.close_position(
            SimpleNamespace(ticker="HOOD", qty=1, option_type="put",
                            contract="HOOD_P", position_type="options_short"),
            1,
        )
        assert captured["action"] == "BUY_TO_CLOSE"

    def test_close_long_equity_sells(self):
        router = OrderRouter(broker=MagicMock())
        captured = {}
        router.execute = lambda o: captured.update(action=o.action) or OrderResult(
            order_id="X", status="dry_run", fill_price=0.0
        )
        router.close_position(
            SimpleNamespace(ticker="AAPL", qty=10, option_type=None,
                            contract=None, position_type="equity_long"),
            10,
        )
        assert captured["action"] == "SELL"

    def test_ladder_leg_model(self):
        leg = LadderLeg(tranche=0, contracts=3, kind="close", status="filled")
        assert leg.contracts == 3
