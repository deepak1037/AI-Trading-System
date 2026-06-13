"""Tests for the Phase 3 bounce instrument selector."""

from __future__ import annotations

from datetime import date

from config.settings import settings
from signals.bounce_instrument_selector import BounceTradeSetup, InstrumentSelector
from signals.bounce_scorer import BounceScore
from signals.drop_classifier import HYBRID, PURE_SENTIMENT


def _score(classification, overall) -> BounceScore:
    return BounceScore(ticker="X", overall_score=overall, classification=classification)


def test_immediate_sentiment_gets_calls() -> None:
    setup = InstrumentSelector().select(
        _score(PURE_SENTIMENT, 80), drop_cause=PURE_SENTIMENT, portfolio_value=100_000
    )
    assert isinstance(setup, BounceTradeSetup)
    assert setup.instrument == "call"
    assert setup.sub_type == "event_call"
    assert setup.delta == settings.BOUNCE_CALL_DELTA
    assert setup.entry_urgency == "IMMEDIATE"
    assert setup.bucket == 3
    # Sizing capped at BUCKET3_MAX_SINGLE_TRADE_PCT of the portfolio.
    assert setup.max_capital == round(
        100_000 * settings.BUCKET3_MAX_SINGLE_TRADE_PCT / 100.0, 2
    )


def test_macro_crash_gets_leap() -> None:
    setup = InstrumentSelector().select(
        _score(HYBRID, 80), drop_cause=HYBRID, regime="crash", portfolio_value=50_000
    )
    assert setup is not None
    assert setup.sub_type == "leap"
    assert setup.delta == settings.BOUNCE_LEAP_DELTA
    assert setup.hold_period == "months"
    assert setup.entry_urgency == "TODAY"


def test_hybrid_without_crash_gets_spread() -> None:
    # High score HYBRID but no crash regime → not a LEAP, defined-risk spread.
    setup = InstrumentSelector().select(
        _score(HYBRID, 80), drop_cause=HYBRID, regime="neutral"
    )
    assert setup is not None
    assert setup.instrument == "call_spread"


def test_medium_confidence_gets_spread() -> None:
    setup = InstrumentSelector().select(_score(HYBRID, 60), drop_cause=HYBRID)
    assert setup is not None
    assert setup.instrument == "call_spread"
    assert setup.profit_target_pct == settings.BOUNCE_SPREAD_PROFIT_TARGET_PCT


def test_low_confidence_returns_none() -> None:
    assert InstrumentSelector().select(_score(HYBRID, 30), drop_cause=HYBRID) is None


def test_expiry_dates_are_future() -> None:
    setup = InstrumentSelector().select(
        _score(PURE_SENTIMENT, 90), drop_cause=PURE_SENTIMENT
    )
    assert setup.expiry > date.today()
    assert setup.expiry.weekday() == 4  # Friday expiry


def test_leap_expiry_is_long_dated() -> None:
    setup = InstrumentSelector().select(
        _score(HYBRID, 85), drop_cause=HYBRID, regime="crash"
    )
    days_out = (setup.expiry - date.today()).days
    assert days_out > 365  # 18-month LEAP


def test_cause_defaults_to_classification() -> None:
    # drop_cause omitted → falls back to the score's classification.
    setup = InstrumentSelector().select(_score(PURE_SENTIMENT, 90))
    assert setup is not None
    assert setup.instrument == "call"
