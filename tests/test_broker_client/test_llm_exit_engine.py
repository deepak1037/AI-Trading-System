"""Tests for broker_client/buckets/llm_exit_engine.py (Step 6)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from broker_client.buckets.llm_exit_engine import (
    ExitReview,
    LLMExitEngine,
    SuggestedAction,
)
from broker_client.buckets.models import BucketPosition
from data.db import get_connection, init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "reviews.db")
    init_db(path)
    return path


@pytest.fixture
def engine(db, monkeypatch):
    # Ensure no real API key leaks in from .env during fallback tests.
    monkeypatch.setattr("broker_client.buckets.llm_exit_engine.settings.ANTHROPIC_API_KEY", "")
    return LLMExitEngine(db_path=db)


def _leap(**kw) -> BucketPosition:
    base = {
        "ticker": "NVDA", "bucket": 3, "sub_type": "leap",
        "dte_remaining": 200, "original_dte": 300, "profit_pct": 45,
    }
    base.update(kw)
    return BucketPosition(**base)


def _fake_anthropic_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


# ── fallback path ────────────────────────────────────────────────────────────
class TestRuleFallback:
    def test_no_api_key_uses_fallback(self, engine):
        review = engine.evaluate_exit(_leap())
        assert review.source == "rule_fallback"
        assert review.recommendation in {"FULL_EXIT", "ROLL_UP", "LADDER", "HOLD"}

    def test_thesis_complete_maps_to_full_exit(self, engine):
        review = engine.evaluate_exit(_leap(thesis_status="complete"))
        assert review.recommendation == "FULL_EXIT"

    def test_roll_action_maps_to_roll_up(self, engine):
        # Bucket 1 ITM near expiry → rules ROLL → ROLL_UP.
        pos = BucketPosition(
            ticker="HOOD", bucket=1, sub_type="msp", dte_remaining=5, profit_pct=5,
            option_type="put", strike=100.0, underlying_price=95.0,
        )
        review = engine.rule_based_review(pos)
        assert review.recommendation == "ROLL_UP"

    def test_fallback_confidence_in_bounds(self, engine):
        review = engine.evaluate_exit(_leap())
        assert 0 <= review.confidence <= 100


# ── LLM path (mocked client) ─────────────────────────────────────────────────
class TestLLMPath:
    def test_evaluate_with_mock_client(self, engine):
        payload = (
            '{"recommendation": "ROLL_UP", "confidence": 82, '
            '"reasoning": "Thesis intact, roll up to capture more.", '
            '"suggested_action": {"description": "Roll to 320 strike", '
            '"sell": "300C", "buy": "320C", "net_credit": 1.25}, '
            '"exit_trigger": "Break below 200MA", "tax_note": "Long-term soon"}'
        )
        client = MagicMock()
        client.messages.create.return_value = _fake_anthropic_response(payload)
        engine._client = client
        review = engine.evaluate_exit(_leap(), {"macro_regime": "long"})
        assert review.source == "llm"
        assert review.recommendation == "ROLL_UP"
        assert review.confidence == 82
        assert review.suggested_action.net_credit == pytest.approx(1.25)
        assert review.macro_regime == "long"

    def test_markdown_fenced_json_parsed(self, engine):
        payload = (
            "```json\n"
            '{"recommendation": "FULL_EXIT", "confidence": 90, "reasoning": "Done.", '
            '"suggested_action": {"description": "Close all"}}\n'
            "```"
        )
        client = MagicMock()
        client.messages.create.return_value = _fake_anthropic_response(payload)
        engine._client = client
        review = engine.evaluate_exit(_leap())
        assert review.recommendation == "FULL_EXIT"
        assert review.confidence == 90

    def test_invalid_recommendation_coerced_to_hold(self, engine):
        payload = '{"recommendation": "MOON", "confidence": 50, "reasoning": "?"}'
        client = MagicMock()
        client.messages.create.return_value = _fake_anthropic_response(payload)
        engine._client = client
        review = engine.evaluate_exit(_leap())
        assert review.recommendation == "HOLD"

    def test_unparseable_response_falls_back(self, engine):
        client = MagicMock()
        client.messages.create.return_value = _fake_anthropic_response("no json here")
        engine._client = client
        review = engine.evaluate_exit(_leap())
        assert review.source == "rule_fallback"

    def test_client_exception_falls_back(self, engine):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("API down")
        engine._client = client
        review = engine.evaluate_exit(_leap())
        assert review.source == "rule_fallback"


# ── persistence + formatting ─────────────────────────────────────────────────
class TestPersistenceAndFormat:
    def test_persist_review_writes_row(self, engine, db):
        review = engine.evaluate_exit(_leap())
        rid = engine.persist_review(review, position_id=7)
        assert rid > 0
        with get_connection(db) as conn:
            row = conn.execute("SELECT * FROM exit_reviews WHERE id=?", (rid,)).fetchone()
        assert row["ticker"] == "NVDA"
        assert row["position_id"] == 7
        assert row["executed"] == 0

    def test_format_discord_contains_recommendation(self):
        review = ExitReview(
            ticker="NVDA", recommendation="ROLL_UP", confidence=80,
            reasoning="Roll it", suggested_action=SuggestedAction(description="Roll to 320"),
            profit_pct=45.0, dte_remaining=200,
        )
        body = LLMExitEngine.format_discord(review)
        assert "ROLL_UP" in body
        assert "+45.0%" in body

    def test_send_review_alert_routes_opportunities(self):
        engine = LLMExitEngine()
        alerts = MagicMock()
        alerts.send_alert.return_value = True
        review = ExitReview(ticker="NVDA", recommendation="HOLD", confidence=50)
        assert engine.send_review_alert(review, alerts) is True
        assert alerts.send_alert.call_args.kwargs["channel"] == "opportunities"

    def test_send_review_alert_no_engine(self):
        assert LLMExitEngine().send_review_alert(
            ExitReview(ticker="X", recommendation="HOLD", confidence=1), None
        ) is False
