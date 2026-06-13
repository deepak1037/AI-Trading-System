"""Tests for the Phase 3 bounce scorer."""

from __future__ import annotations

from signals.bounce_scorer import BounceScore, BounceScorer
from signals.drop_classifier import (
    FUNDAMENTAL,
    HYBRID,
    PURE_SENTIMENT,
    DropClassification,
)


def _cls(classification, **kw) -> DropClassification:
    base = dict(
        ticker=kw.pop("ticker", "X"),
        drop_pct=kw.pop("drop_pct", 6.0),
        classification=classification,
        institutional_action=kw.pop("institutional_action", "neutral"),
        sector_peers=kw.pop("sector_peers", "isolated"),
        fundamental_signals=kw.pop("fundamental_signals", []),
    )
    base.update(kw)
    return DropClassification(**base)


def test_high_score_pure_sentiment() -> None:
    score = BounceScorer().score(_cls(PURE_SENTIMENT, institutional_action="buying"))
    assert score.overall_score >= 75
    assert score.trade_recommendation == "STRONG_BUY"
    assert score.suggested_instrument == "calls"
    assert score.urgency == "IMMEDIATE"
    assert score.suggested_bucket == 3


def test_medium_score_hybrid() -> None:
    # A clean isolated hybrid scores high; sector-wide drag pulls it to medium.
    score = BounceScorer().score(_cls(HYBRID, sector_peers="sector_wide"))
    assert 55 <= score.overall_score < 75
    assert score.trade_recommendation == "BUY"
    assert score.suggested_instrument == "call_spread"


def test_low_score_fundamental() -> None:
    score = BounceScorer().score(
        _cls(FUNDAMENTAL, fundamental_signals=["Revenue miss", "Guidance cut"])
    )
    assert score.cause_score == 10
    assert score.trade_recommendation == "SKIP"
    assert score.suggested_instrument == "none"
    assert score.overall_score < 55


def test_institutional_buying_boosts_score() -> None:
    buying = BounceScorer().score(_cls(HYBRID, institutional_action="buying"))
    selling = BounceScorer().score(_cls(HYBRID, institutional_action="selling"))
    assert buying.institutional_score > selling.institutional_score
    assert buying.overall_score > selling.overall_score


def test_sector_wide_drop_reduces_score() -> None:
    isolated = BounceScorer().score(_cls(PURE_SENTIMENT, sector_peers="isolated"))
    sector = BounceScorer().score(_cls(PURE_SENTIMENT, sector_peers="sector_wide"))
    assert isolated.timing_score > sector.timing_score
    assert isolated.overall_score > sector.overall_score


def test_rsi_oversold_boosts_technical() -> None:
    oversold = BounceScorer().score(_cls(HYBRID), rsi=25)
    overbought = BounceScorer().score(_cls(HYBRID), rsi=75)
    assert oversold.technical_score == 90
    assert overbought.technical_score == 20


def test_technical_score_falls_back_to_drop_magnitude() -> None:
    small = BounceScorer().score(_cls(HYBRID, drop_pct=4.0))
    big = BounceScorer().score(_cls(HYBRID, drop_pct=10.0))
    assert big.technical_score > small.technical_score


def test_score_is_bounded() -> None:
    score = BounceScorer().score(
        _cls(PURE_SENTIMENT, institutional_action="buying", drop_pct=30.0), rsi=10
    )
    assert 0 <= score.overall_score <= 100
    assert isinstance(score, BounceScore)
