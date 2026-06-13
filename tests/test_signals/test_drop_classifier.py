"""Tests for the Phase 3 drop classifier (sentiment vs fundamental)."""

from __future__ import annotations

from unittest.mock import MagicMock

from config.settings import settings
from signals.drop_classifier import (
    EARNINGS_MISS,
    FUNDAMENTAL,
    HYBRID,
    PURE_SENTIMENT,
    DropClassification,
    DropClassifier,
)


def _classify(clf: DropClassifier, ticker, drop, headlines, **kw):
    # Force the deterministic path (peers supplied, LLM off) for stable tests.
    kw.setdefault("peer_action", "isolated")
    kw.setdefault("use_llm", False)
    return clf.classify(ticker, drop, headlines, **kw)


def test_pure_sentiment_tsla_tweet() -> None:
    clf = DropClassifier()
    headlines = [
        "Tesla drops as Musk posts controversial tweet about politics",
        "Social media feud sparks TSLA selloff, no business impact",
    ]
    res = _classify(clf, "TSLA", 6.0, headlines)
    assert res.classification == PURE_SENTIMENT
    assert res.bounce_confidence >= 75
    assert res.recovery_expected is True
    assert res.sentiment_signals


def test_hybrid_nvda_export_restriction() -> None:
    clf = DropClassifier()
    headlines = [
        "NVDA falls on H20 chip export restriction to China",
        "Company takes one-time charge; new compliant chip already ready",
    ]
    res = _classify(clf, "NVDA", 8.0, headlines)
    assert res.classification == HYBRID
    assert res.recovery_expected is True
    assert res.recovery_timeframe == "weeks"
    assert 40 <= res.bounce_confidence <= 90


def test_fundamental_revenue_miss() -> None:
    clf = DropClassifier()
    headlines = [
        "Acme reports a big revenue miss vs estimates",
        "Company guidance lowered for the full year",
    ]
    res = _classify(clf, "ACME", 12.0, headlines)
    assert res.classification == FUNDAMENTAL
    assert res.recovery_expected is False
    assert res.bounce_confidence <= 15        # never inflate a fundamental bounce


def test_one_time_charge_detection() -> None:
    out = DropClassifier._check_one_time_vs_recurring(
        ["One-time impairment charge and a regulatory settlement"]
    )
    assert out["is_one_time"] is True
    assert out["is_recurring"] is False

    out2 = DropClassifier._check_one_time_vs_recurring(
        ["Ongoing margin compression as competition gaining share"]
    )
    assert out2["is_recurring"] is True


def test_recurring_routes_to_fundamental() -> None:
    clf = DropClassifier()
    res = _classify(
        clf, "X", 9.0,
        ["Permanent ban and secular decline in the core market"],
    )
    assert res.classification == FUNDAMENTAL


def test_earnings_miss_classification() -> None:
    clf = DropClassifier()
    res = _classify(
        clf, "X", 7.0,
        ["Stock slips after quarterly results, roughly in-line earnings"],
    )
    assert res.classification == EARNINGS_MISS


def test_peer_comparison_isolated_vs_sector() -> None:
    assert DropClassifier._peer_bucket(0.5) == "isolated"
    assert DropClassifier._peer_bucket(-1.0) == "partial_sector"
    assert DropClassifier._peer_bucket(-3.0) == "sector_wide"


def test_sector_wide_lowers_bounce_confidence() -> None:
    clf = DropClassifier()
    headlines = ["Musk tweet drama sparks selloff"]
    isolated = _classify(clf, "TSLA", 6.0, headlines, peer_action="isolated")
    sector = _classify(clf, "TSLA", 6.0, headlines, peer_action="sector_wide")
    assert isolated.bounce_confidence > sector.bounce_confidence


def test_institutional_buying_boosts_bounce() -> None:
    clf = DropClassifier()
    headlines = ["Musk tweet drama sparks selloff"]
    buying = _classify(clf, "TSLA", 6.0, headlines, institutional_action="buying")
    selling = _classify(clf, "TSLA", 6.0, headlines, institutional_action="selling")
    assert buying.bounce_confidence > selling.bounce_confidence


def test_fundamental_signals_detected() -> None:
    sigs = DropClassifier._check_fundamental_signals(
        ["Revenue miss vs estimates", "Company guidance lowered", "Analyst downgrade"]
    )
    assert any("Revenue miss" in s for s in sigs)
    assert any("guidance" in s.lower() for s in sigs)
    assert len(sigs) >= 2


def test_no_news_returns_ambiguous_hybrid() -> None:
    clf = DropClassifier()
    res = _classify(clf, "X", 5.0, [])
    assert res.classification == HYBRID  # nothing detected → ambiguous
    assert isinstance(res, DropClassification)


# ── LLM path ──────────────────────────────────────────────────────────────────────
def test_llm_fallback_on_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    clf = DropClassifier()
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(clf, "_ensure_client", lambda: client)
    res = clf.classify("TSLA", 6.0, ["Musk tweet drama"], peer_action="isolated")
    assert res.model == "rule_based"           # fell back
    assert res.classification == PURE_SENTIMENT


def test_llm_path_overrides_when_available(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    clf = DropClassifier()
    payload = (
        '{"classification": "HYBRID", "confidence": 82, "cause_summary": "one-time",'
        ' "recovery_expected": true, "recovery_timeframe": "weeks",'
        ' "bounce_confidence": 70, "reasoning": "market overreacted"}'
    )
    block = MagicMock()
    block.type = "text"
    block.text = payload
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    monkeypatch.setattr(clf, "_ensure_client", lambda: client)
    res = clf.classify("NVDA", 8.0, ["export restriction one-time charge"], peer_action="isolated")
    assert res.model == settings.LLM_MODEL
    assert res.classification == HYBRID
    assert res.confidence == 82
