"""Tests for broker_client/options_engine.py and options strategies (Day 10)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from broker_client.options_engine import OptionsEngine
from broker_client.strategies.cash_secured_put import CashSecuredPut
from broker_client.strategies.covered_call import CoveredCall
from broker_client.strategies.iron_condor import IronCondor
from broker_client.strategies.protective_put import ProtectivePut
from broker_core.base_broker import Account, OptionsChain, OptionsContract
from signals.signal_schema import Signal


def _make_chain(ticker="AAPL", current_price=150.0) -> OptionsChain:
    """Build a realistic mock options chain."""
    strikes = [135, 140, 145, 150, 155, 160, 165]
    calls = [
        OptionsContract(
            symbol=f"{ticker}240119C{int(s * 1000):08d}",
            expiry="2024-01-19",
            strike=float(s),
            option_type="call",
            bid=max(0, (current_price - s + 5) * 0.8),
            ask=max(0, (current_price - s + 5) * 1.1),
            delta=max(0.01, 0.8 - (s - current_price) * 0.03),
        )
        for s in strikes
    ]
    puts = [
        OptionsContract(
            symbol=f"{ticker}240119P{int(s * 1000):08d}",
            expiry="2024-01-19",
            strike=float(s),
            option_type="put",
            bid=max(0, (s - current_price + 5) * 0.8),
            ask=max(0, (s - current_price + 5) * 1.1),
            delta=min(-0.01, -0.8 + (s - current_price) * 0.03),
        )
        for s in strikes
    ]
    return OptionsChain(ticker=ticker, expiry="2024-01-19", calls=calls, puts=puts)


@pytest.fixture
def engine():
    return OptionsEngine()


@pytest.fixture
def chain():
    return _make_chain()


class TestOptionsEngineStrikeSelection:
    def test_select_otm_call(self, engine, chain):
        call = engine.select_otm_call(chain, current_price=150.0, otm_pct=0.05)
        assert call is not None
        assert call.strike >= 150.0
        # Target = 157.5 → closest available = 160
        assert call.strike in (155.0, 160.0)

    def test_select_otm_put(self, engine, chain):
        put = engine.select_otm_put(chain, current_price=150.0, otm_pct=0.05)
        assert put is not None
        assert put.strike <= 150.0
        assert put.strike in (140.0, 145.0)

    def test_select_delta_call(self, engine, chain):
        call = engine.select_delta_call(chain, target_delta=0.30)
        assert call is not None
        assert call.delta is not None

    def test_select_delta_put(self, engine, chain):
        put = engine.select_delta_put(chain, target_delta=-0.30)
        assert put is not None
        assert put.delta is not None

    def test_empty_chain_returns_none(self, engine):
        empty = OptionsChain(ticker="SPY", expiry="2024-01-19")
        assert engine.select_otm_call(empty, 450.0) is None
        assert engine.select_otm_put(empty, 450.0) is None

    def test_mid_price(self, engine, chain):
        call = chain.calls[0]
        mid = engine.mid_price(call)
        assert mid == pytest.approx((call.bid + call.ask) / 2)

    def test_build_sell_call_order(self, engine, chain):
        call = chain.calls[-1]
        order = engine.build_sell_call_order("AAPL", call)
        assert order.action == "SELL_TO_OPEN"
        assert order.ticker == "AAPL"
        assert order.qty == 1

    def test_build_buy_put_order(self, engine, chain):
        put = chain.puts[0]
        order = engine.build_buy_put_order("AAPL", put)
        assert order.action == "BUY_TO_OPEN"


def _acct(equity=100000.0) -> Account:
    return Account(account_id="paper_main", cash=equity, equity=equity, buying_power=equity)


def _sig(direction="neutral", confidence=70, source="fusion", ticker="AAPL") -> Signal:
    return Signal(
        direction=direction,
        confidence=confidence,
        source=source,
        timestamp=datetime.now(tz=timezone.utc),
        metadata={"ticker": ticker},
    )


class TestOptionsStrategies:
    def test_covered_call_enters_on_neutral(self):
        strat = CoveredCall()
        assert strat.should_enter(_sig("neutral", 70, "fusion"), _acct()) is True

    def test_covered_call_no_entry_on_bearish(self):
        strat = CoveredCall()
        assert strat.should_enter(_sig("short", 80, "fusion"), _acct()) is False

    def test_cash_secured_put_enters_on_neutral(self):
        strat = CashSecuredPut()
        assert strat.should_enter(_sig("neutral", 70, "fusion"), _acct()) is True

    def test_protective_put_enters_on_bearish(self):
        strat = ProtectivePut()
        assert strat.should_enter(_sig("short", 80, "fusion"), _acct()) is True

    def test_protective_put_no_entry_on_bullish(self):
        strat = ProtectivePut()
        assert strat.should_enter(_sig("long", 80, "fusion"), _acct()) is False

    def test_iron_condor_enters_on_neutral(self):
        strat = IronCondor()
        assert strat.should_enter(_sig("neutral", 60, "fusion"), _acct()) is True

    def test_iron_condor_no_entry_on_directional(self):
        strat = IronCondor()
        assert strat.should_enter(_sig("long", 80, "fusion"), _acct()) is False

    def test_all_strategies_describe(self):
        for cls in [CoveredCall, CashSecuredPut, ProtectivePut, IronCondor]:
            desc = cls().describe()
            assert isinstance(desc, str) and len(desc) > 0

    def test_all_strategies_in_registry(self):
        from broker_client.strategies import STRATEGY_REGISTRY
        for name in ["covered_call", "cash_secured_put", "protective_put", "iron_condor"]:
            assert name in STRATEGY_REGISTRY
