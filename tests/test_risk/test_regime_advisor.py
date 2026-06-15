"""Tests for broker_client/risk/regime_advisor.py."""

from __future__ import annotations

from broker_client.risk.regime_advisor import RegimeAdvisor


class _State:
    def __init__(self, score: int, regime: str = "neutral"):
        self.composite_score = score
        self.current_regime = regime


def test_bullish_regime_aggressive_guidance():
    advisor = RegimeAdvisor()
    advice = advisor.advise(_State(70))
    assert advice.regime == "bull"
    assert advice.bucket1_adjustment == "aggressive"
    assert advice.put_otm_buffer_multiplier < 1.0
    assert len(advice.guidance) > 0


def test_bearish_regime_conservative_guidance():
    advisor = RegimeAdvisor()
    advice = advisor.advise(_State(30))
    assert advice.regime == "bear"
    assert advice.bucket1_adjustment == "conservative"
    assert advice.bucket3_adjustment == "reduced"
    assert advice.put_otm_buffer_multiplier > 1.0
    assert len(advice.warnings) > 0


def test_choppy_regime_earnings_preferred():
    advisor = RegimeAdvisor()
    advice = advisor.advise(_State(50))
    assert advice.regime == "choppy"
    assert advice.bucket1_adjustment == "normal"
    assert advice.bucket3_adjustment == "reduced"
    assert any("earnings" in g.lower() for g in advice.guidance)


def test_boundary_at_65():
    advisor = RegimeAdvisor()
    assert advisor.advise(_State(65)).regime == "bull"
    assert advisor.advise(_State(64)).regime == "choppy"


def test_boundary_at_35():
    advisor = RegimeAdvisor()
    assert advisor.advise(_State(35)).regime == "bear"
    assert advisor.advise(_State(36)).regime == "choppy"


def test_none_market_state_defaults_to_choppy():
    advisor = RegimeAdvisor()
    advice = advisor.advise(None)
    assert advice.regime == "choppy"
