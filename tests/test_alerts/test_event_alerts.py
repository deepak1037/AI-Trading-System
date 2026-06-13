"""Tests for the Phase 3 earnings + bounce opportunity alerts."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from alerts.alert_engine import AlertEngine
from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    EarningsAssessment,
    EarningsOpportunity,
    IVAnalysis,
    IVCrushTrade,
    IVSpikeTrade,
)
from signals.bounce_instrument_selector import BounceTradeSetup
from signals.bounce_scorer import BounceScore
from signals.drop_classifier import HYBRID, PURE_SENTIMENT, DropClassification


def _future(days: int) -> date:
    return date.today() + timedelta(days=days)


def _crush_opp() -> EarningsOpportunity:
    return EarningsOpportunity(
        ticker="NVDA",
        earnings_date=_future(10),
        earnings_time="AMC",
        strategy=IV_CRUSH,
        iv_analysis=IVAnalysis(
            ticker="NVDA", iv_rank_current=70, expected_move_current=8.0,
            avg_iv_crush=24.0, breach_rate=0.1, strategy=IV_CRUSH,
        ),
        trade=IVCrushTrade(
            ticker="NVDA", earnings_date=_future(10), put_strike=120.0,
            put_expiry=_future(12), put_premium=150.0, margin_required=1500.0,
            roi_margin=0.10, break_even_pct=12.0,
        ),
        llm_assessment=EarningsAssessment(
            recommendation="EXECUTE", confidence=85, reasoning="reliable crusher",
        ),
        overall_score=82,
        priority="HIGH",
    )


def _spike_opp() -> EarningsOpportunity:
    return EarningsOpportunity(
        ticker="TSLA",
        earnings_date=_future(10),
        strategy=IV_SPIKE,
        iv_analysis=IVAnalysis(ticker="TSLA", iv_rank_current=20, strategy=IV_SPIKE),
        trade=IVSpikeTrade(
            ticker="TSLA", earnings_date=_future(10), entry_date=_future(3),
            exit_date=_future(9), call_strike=200.0, put_strike=200.0,
            expiry=_future(11), total_premium_paid=2000.0, max_loss=2000.0,
        ),
        overall_score=70,
        priority="MEDIUM",
    )


def test_send_earnings_alert_crush() -> None:
    engine = AlertEngine()
    engine.send_alert = MagicMock(return_value=True)
    assert engine.send_earnings_alert(_crush_opp()) is True
    kwargs = engine.send_alert.call_args.kwargs
    assert kwargs["channel"] == "opportunities"
    assert kwargs["color"] == "green"
    assert "NVDA" in kwargs["title"]
    body = kwargs["body"]
    assert "IV_CRUSH" in body
    assert "Sell 120P" in body
    assert "EXECUTE" in body
    assert "82/100" in body


def test_send_earnings_alert_spike_shows_exit_warning() -> None:
    engine = AlertEngine()
    engine.send_alert = MagicMock(return_value=True)
    engine.send_earnings_alert(_spike_opp())
    body = engine.send_alert.call_args.kwargs["body"]
    assert "BEFORE earnings" in body
    assert str(_future(9)) in body  # the hard exit date


def test_send_bounce_alert() -> None:
    engine = AlertEngine()
    engine.send_alert = MagicMock(return_value=True)
    classification = DropClassification(
        ticker="TSLA", drop_pct=6.0, classification=PURE_SENTIMENT,
        cause_summary="Musk tweet drama", bounce_confidence=85,
        institutional_action="buying",
    )
    score = BounceScore(ticker="TSLA", overall_score=82, classification=PURE_SENTIMENT)
    setup = BounceTradeSetup(
        ticker="TSLA", instrument="call", profit_target_pct=50.0,
        max_capital=2000.0, entry_urgency="IMMEDIATE",
        thesis_complete_signal="Price reclaims pre-drop level",
    )
    assert engine.send_bounce_alert(classification, score, setup) is True
    kwargs = engine.send_alert.call_args.kwargs
    assert kwargs["channel"] == "opportunities"
    body = kwargs["body"]
    assert "PURE_SENTIMENT" in body
    assert "Musk tweet drama" in body
    assert "Bucket 3" in body
    assert "IMMEDIATE" in body


def test_send_bounce_alert_without_trade() -> None:
    engine = AlertEngine()
    engine.send_alert = MagicMock(return_value=True)
    classification = DropClassification(
        ticker="X", drop_pct=5.0, classification=HYBRID, bounce_confidence=40,
    )
    score = BounceScore(ticker="X", overall_score=45, classification=HYBRID)
    engine.send_bounce_alert(classification, score, None)
    body = engine.send_alert.call_args.kwargs["body"]
    assert "SUGGESTED TRADE" not in body  # no setup → no trade block
