"""Tests for the Phase 3 earnings strategy builder (IV crush + spike)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsEvent,
    IVAnalysis,
    IVCrushTrade,
    IVSpikeTrade,
)
from broker_client.earnings.strategy_builder import EarningsStrategyBuilder
from broker_client.llm_roi_analyzer import PutROIResult


def _future(days: int) -> date:
    return date.today() + timedelta(days=days)


def _crush_event(ticker: str = "NVDA", price: float = 135.0) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker,
        earnings_date=_future(10),
        earnings_time="AMC",
        iv_rank=70,
        iv_percentile=70,
        expected_move=8.0,
        stock_price=price,
    )


def _crush_analysis() -> IVAnalysis:
    return IVAnalysis(
        ticker="NVDA",
        avg_iv_crush=25.0,
        iv_crush_consistency=0.9,
        breach_rate=0.1,
        breach_rate_lower=0.05,
        safe_move_level=10.0,
        iv_rank_current=70,
        iv_percentile_current=70,
        expected_move_current=8.0,
        strategy=IV_CRUSH,
        confidence=80,
    )


def _spike_analysis() -> IVAnalysis:
    return IVAnalysis(
        ticker="TSLA",
        avg_actual_move=12.0,
        breach_rate=0.6,
        iv_rank_current=20,
        iv_percentile_current=30,
        expected_move_current=10.0,
        strategy=IV_SPIKE,
        confidence=70,
    )


# ── strike selection ────────────────────────────────────────────────────────────
def test_put_strike_safety_buffer() -> None:
    # NVDA $135, max(8, 10)=10 * 1.1 buffer = 11% → 135*0.89 = 120.15 → round $5 = 120.
    strike = EarningsStrategyBuilder.select_put_strike(135.0, 8.0, 10.0, safety_buffer=1.1)
    assert strike == 120.0


def test_put_strike_uses_expected_when_larger() -> None:
    strike = EarningsStrategyBuilder.select_put_strike(100.0, 20.0, 5.0, safety_buffer=1.0)
    # max(20,5)=20% → 100*0.80 = 80
    assert strike == 80.0


# ── IV crush construction ─────────────────────────────────────────────────────────
def test_iv_crush_trade_construction() -> None:
    builder = EarningsStrategyBuilder()  # no broker → estimate path
    trade = builder.build_iv_crush_trade(_crush_event(), _crush_analysis())
    assert isinstance(trade, IVCrushTrade)
    assert trade.strategy == IV_CRUSH
    assert trade.put_strike < 135.0          # OTM
    assert trade.put_premium > 0
    assert trade.margin_required > 0
    assert trade.margin_basis == "estimate"
    assert trade.put_expiry > trade.earnings_date  # captures the crush
    assert trade.break_even_pct > 0
    assert trade.put_breach_probability == 0.05


def test_iv_crush_expiry_is_friday_after_earnings() -> None:
    builder = EarningsStrategyBuilder()
    trade = builder.build_iv_crush_trade(_crush_event(), _crush_analysis())
    assert trade.put_expiry.weekday() == 4     # Friday
    assert trade.put_expiry >= trade.earnings_date


def test_build_dispatch_skip_returns_none() -> None:
    builder = EarningsStrategyBuilder()
    analysis = _crush_analysis()
    analysis.strategy = SKIP
    assert builder.build(_crush_event(), analysis) is None


def test_iv_crush_no_price_returns_none() -> None:
    builder = EarningsStrategyBuilder()
    event = _crush_event()
    event.stock_price = None
    assert builder.build_iv_crush_trade(event, _crush_analysis()) is None


# ── ROI / Schwab margin path ──────────────────────────────────────────────────────
class _StubROIAnalyzer:
    """Stands in for LLMROIAnalyzer.build_put_roi with deterministic numbers."""

    def __init__(self, premium: float, margin: float) -> None:
        self.premium = premium
        self.margin = margin

    def build_put_roi(self, ticker, strike, expiry, use_broker_margin=False):
        return PutROIResult(
            ticker=ticker, strike=strike, expiry=expiry, days_to_expiry=5,
            underlying_price=135.0, bid=1.0, ask=1.1, mid=1.05,
            gross_premium_per_contract=self.premium + 0.65,
            commission_per_contract=0.65,
            premium_per_contract=self.premium,
            margin_per_contract=self.margin, margin_basis="schwab_preview",
            breakeven=119.0, otm_pct=11.0,
            static_roi_pct=1.0, monthly_roi_pct=6.0, annualized_roi_pct=72.0,
            cash_secured_margin_per_contract=strike * 100,
            cash_secured_roi_pct=0.5, cash_secured_monthly_roi_pct=3.0,
            cash_secured_annualized_roi_pct=36.0,
        )


def test_roi_calculation_matches_schwab() -> None:
    # Broker present + stub analyzer → real-margin path; ROI = premium / margin.
    builder = EarningsStrategyBuilder(
        broker=object(), roi_analyzer=_StubROIAnalyzer(premium=150.0, margin=1500.0)
    )
    trade = builder.build_iv_crush_trade(_crush_event(), _crush_analysis())
    assert trade.margin_basis == "schwab_preview"
    assert trade.margin_required == 1500.0
    assert trade.put_premium == 150.0
    assert trade.roi_margin == pytest.approx(0.10)        # 150 / 1500
    assert trade.roi_cash_secured == pytest.approx(150.0 / (trade.put_strike * 100))


# ── IV spike construction ─────────────────────────────────────────────────────────
def test_iv_spike_exit_date_before_earnings() -> None:
    builder = EarningsStrategyBuilder()
    event = EarningsEvent(
        ticker="TSLA", earnings_date=_future(10), expected_move=10.0, stock_price=200.0,
    )
    trade = builder.build_iv_spike_trade(event, _spike_analysis())
    assert isinstance(trade, IVSpikeTrade)
    assert trade.exit_date < trade.earnings_date     # the hard rule
    assert trade.entry_date < trade.exit_date
    assert trade.instrument == "straddle"
    assert trade.max_loss == trade.total_premium_paid
    assert trade.call_strike == trade.put_strike == 200.0  # ATM, rounded $5


def test_iv_spike_too_close_returns_none() -> None:
    # Earnings tomorrow → can't carve a valid entry<exit<earnings window.
    builder = EarningsStrategyBuilder()
    event = EarningsEvent(
        ticker="X", earnings_date=date.today(), expected_move=10.0, stock_price=100.0,
    )
    assert builder.build_iv_spike_trade(event, _spike_analysis()) is None


def test_iv_spike_premium_tracks_expected_move() -> None:
    builder = EarningsStrategyBuilder()
    event = EarningsEvent(
        ticker="TSLA", earnings_date=_future(10), expected_move=10.0, stock_price=200.0,
    )
    trade = builder.build_iv_spike_trade(event, _spike_analysis())
    # straddle ≈ price * em% * 100 = 200 * 0.10 * 100 = 2000
    assert trade.total_premium_paid == pytest.approx(2000.0)
