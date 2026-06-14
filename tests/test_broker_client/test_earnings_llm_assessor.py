"""Tests for the Phase 3 earnings LLM assessor + rule-based fallback."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock


from broker_client.earnings.llm_assessor import LLMEarningsAssessor
from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsAssessment,
    IVAnalysis,
    IVCrushTrade,
    IVSpikeTrade,
)
from config.settings import settings


def _future(days: int) -> date:
    return date.today() + timedelta(days=days)


def _crush_trade() -> IVCrushTrade:
    return IVCrushTrade(
        ticker="NVDA", earnings_date=_future(10), put_strike=120.0,
        put_expiry=_future(12), put_premium=150.0, margin_required=1500.0,
        margin_basis="schwab_preview", roi_margin=0.10, break_even_pct=12.0,
    )


def _spike_trade() -> IVSpikeTrade:
    return IVSpikeTrade(
        ticker="TSLA", earnings_date=_future(10), entry_date=_future(3),
        exit_date=_future(9), call_strike=200.0, put_strike=200.0, expiry=_future(11),
        total_premium_paid=2000.0, max_loss=2000.0, target_profit_pct=30.0,
    )


def _crush_analysis(confidence: int = 80) -> IVAnalysis:
    return IVAnalysis(
        ticker="NVDA", avg_iv_crush=25.0, iv_crush_consistency=0.9,
        breach_rate=0.1, breach_rate_lower=0.05, iv_rank_current=70,
        strategy=IV_CRUSH, confidence=confidence,
    )


def _spike_analysis(confidence: int = 70) -> IVAnalysis:
    return IVAnalysis(
        ticker="TSLA", avg_actual_move=12.0, breach_rate=0.6,
        iv_rank_current=20, strategy=IV_SPIKE, confidence=confidence,
    )


# ── rule-based fallback (no API key) ──────────────────────────────────────────────
def test_fallback_when_no_api_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    a = LLMEarningsAssessor().assess("NVDA", _crush_analysis(), _crush_trade())
    assert a.model == "rule_based"
    assert a.recommendation == "EXECUTE"      # confidence 80 >= critical
    assert a.sizing_suggestion == "full"
    assert a.probability_of_profit == 95       # (1 - 0.05) * 100
    assert a.key_strengths and a.key_risks


def test_fallback_spike_exit_plan_before_earnings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    trade = _spike_trade()
    a = LLMEarningsAssessor().assess("TSLA", _spike_analysis(), trade)
    assert "before earnings" in a.exit_plan.lower()
    assert str(trade.exit_date) in a.exit_plan


def test_fallback_half_size_on_elevated_downside_breach(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    # ACN-like: high confidence but downside breach 25% (3 of 12) → half size.
    analysis = IVAnalysis(
        ticker="ACN", avg_iv_crush=10.0, iv_crush_consistency=0.8,
        breach_rate=0.42, breach_rate_lower=0.25, quarters_analyzed=12,
        iv_rank_current=99, strategy=IV_CRUSH, confidence=85,
    )
    a = LLMEarningsAssessor().assess("ACN", analysis, _crush_trade())
    assert a.recommendation == "EXECUTE"
    assert a.sizing_suggestion == "half"          # downgraded from full
    assert "3 of 12 quarters breached to the downside" in a.reasoning
    assert "25%" in a.reasoning


def test_fallback_full_size_when_downside_breach_low(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    analysis = IVAnalysis(
        ticker="NVDA", avg_iv_crush=25.0, iv_crush_consistency=0.9,
        breach_rate=0.1, breach_rate_lower=0.05, quarters_analyzed=12,
        iv_rank_current=70, strategy=IV_CRUSH, confidence=85,
    )
    a = LLMEarningsAssessor().assess("NVDA", analysis, _crush_trade())
    assert a.recommendation == "EXECUTE"
    assert a.sizing_suggestion == "full"          # low breach → full size


def test_fallback_crush_executes_sized_down_on_low_confidence(monkeypatch) -> None:
    # An approved IV_CRUSH always EXECUTEs (analyzer already vetted it); low
    # confidence drives smaller SIZE, not a SKIP.
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    a = LLMEarningsAssessor().assess("NVDA", _crush_analysis(confidence=30), _crush_trade())
    assert a.recommendation == "EXECUTE"
    assert a.sizing_suggestion == "half"      # low confidence → reduced size


def test_fallback_mid_confidence_executes_half(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    a = LLMEarningsAssessor().assess("NVDA", _crush_analysis(confidence=55), _crush_trade())
    assert a.recommendation == "EXECUTE"
    assert a.sizing_suggestion == "half"      # < CONFIDENCE_HIGH and low breach → half


def test_no_trade_returns_skip() -> None:
    a = LLMEarningsAssessor().assess("X", IVAnalysis(ticker="X", strategy=SKIP), None)
    assert a.recommendation == "SKIP"
    assert a.confidence == 0


# ── LLM path (mocked client) ───────────────────────────────────────────────────────
def test_llm_path_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    payload = (
        '{"recommendation": "EXECUTE", "confidence": 88, "probability_of_profit": 75,'
        ' "key_risks": ["gap"], "key_strengths": ["crush"], "sizing_suggestion": "full",'
        ' "reasoning": "good", "exit_plan": "close at 50%", "watch_for": "miss"}'
    )
    block = MagicMock()
    block.type = "text"
    block.text = payload
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp

    assessor = LLMEarningsAssessor()
    monkeypatch.setattr(assessor, "_ensure_client", lambda: client)
    a = assessor.assess("NVDA", _crush_analysis(), _crush_trade())
    assert a.recommendation == "EXECUTE"
    assert a.confidence == 88
    assert a.model == settings.LLM_MODEL


def test_llm_path_falls_back_on_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    assessor = LLMEarningsAssessor()
    monkeypatch.setattr(assessor, "_ensure_client", lambda: client)
    a = assessor.assess("NVDA", _crush_analysis(), _crush_trade())
    assert a.model == "rule_based"      # fell back
    assert a.recommendation == "EXECUTE"


def test_llm_path_falls_back_on_bad_json(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    block = MagicMock()
    block.type = "text"
    block.text = "no json here"
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    assessor = LLMEarningsAssessor()
    monkeypatch.setattr(assessor, "_ensure_client", lambda: client)
    a = assessor.assess("NVDA", _crush_analysis(), _crush_trade())
    assert a.model == "rule_based"


def test_parse_strips_markdown_fence() -> None:
    text = '```json\n{"recommendation": "SKIP", "confidence": 10}\n```'
    a = LLMEarningsAssessor._parse(text)
    assert isinstance(a, EarningsAssessment)
    assert a.recommendation == "SKIP"
    assert a.confidence == 10
