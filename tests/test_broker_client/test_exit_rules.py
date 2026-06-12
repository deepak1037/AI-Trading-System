"""Tests for broker_client/buckets/exit_rules.py (Step 4)."""

from __future__ import annotations

import pytest

from broker_client.buckets.exit_rules import (
    FULL_EXIT,
    HOLD,
    IMMEDIATE,
    LLM_REVIEW,
    ROLL,
    STOP_LOSS,
    ExitRulesEngine,
)
from broker_client.buckets.models import BucketPosition


@pytest.fixture
def engine():
    return ExitRulesEngine()


def _pos(**kw) -> BucketPosition:
    base = {"ticker": "TEST", "bucket": 1, "sub_type": "msp"}
    base.update(kw)
    return BucketPosition(**base)


# ── Bucket 1 ─────────────────────────────────────────────────────────────────
class TestBucket1:
    def test_short_dte_50pct_profit_full_exit(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=10, profit_pct=55))
        assert rec.action == FULL_EXIT
        assert rec.urgency == IMMEDIATE

    def test_short_dte_70pct_near_expiry(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=2, profit_pct=72))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b1_short_fast_profit"

    def test_expiry_day_rolls(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=1, profit_pct=10))
        assert rec.action == ROLL
        assert rec.urgency == IMMEDIATE

    def test_itm_within_7dte_rolls(self, engine):
        rec = engine.check_exit(_pos(
            bucket=1, dte_remaining=5, profit_pct=10,
            option_type="put", strike=100.0, underlying_price=95.0,
        ))
        assert rec.action == ROLL
        assert rec.rule_triggered == "b1_itm_roll"

    def test_medium_dte_50pct(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=30, profit_pct=51))
        assert rec.action == FULL_EXIT
        assert rec.urgency == "TODAY"

    def test_21dte_rule_exits_regardless_of_profit(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=20, profit_pct=5))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b1_21dte"

    def test_long_dte_early_25pct(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=50, profit_pct=26, days_held=4))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b1_long_early"

    def test_healthy_position_holds(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=50, profit_pct=10, days_held=20))
        assert rec.action == HOLD

    def test_never_stop_loss_even_deep_loss(self, engine):
        # A large unrealized loss on Bucket 1 must NEVER yield STOP_LOSS.
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=30, profit_pct=-300))
        assert rec.action != STOP_LOSS

    def test_never_stop_loss_short_dte_loss(self, engine):
        rec = engine.check_exit(_pos(bucket=1, dte_remaining=5, profit_pct=-150))
        assert rec.action != STOP_LOSS


# ── Bucket 2A — earnings put ──────────────────────────────────────────────────
class TestBucket2A:
    def test_pre_earnings_exit(self, engine):
        rec = engine.check_exit(_pos(
            bucket=2, sub_type="earnings_put", dte_remaining=30, profit_pct=10,
            days_to_earnings=1,
        ))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b2a_pre_earnings"

    def test_post_earnings_exit(self, engine):
        rec = engine.check_exit(_pos(
            bucket=2, sub_type="earnings_put", dte_remaining=30, profit_pct=10,
            days_to_earnings=-1,
        ))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b2a_post_earnings"

    def test_falls_back_to_bucket1_rules(self, engine):
        rec = engine.check_exit(_pos(
            bucket=2, sub_type="earnings_put", dte_remaining=10, profit_pct=55,
        ))
        assert rec.action == FULL_EXIT  # B1 50% short-DTE rule


# ── Bucket 2B — defined risk ──────────────────────────────────────────────────
class TestBucket2B:
    def test_50pct_profit_full_exit(self, engine):
        rec = engine.check_exit(_pos(bucket=2, sub_type="earnings_spread",
                                     dte_remaining=30, profit_pct=55))
        assert rec.action == FULL_EXIT

    def test_stop_loss_at_200pct(self, engine):
        rec = engine.check_exit(_pos(bucket=2, sub_type="earnings_spread",
                                     dte_remaining=30, profit_pct=-210))
        assert rec.action == STOP_LOSS
        assert rec.urgency == IMMEDIATE

    def test_21dte_exit(self, engine):
        rec = engine.check_exit(_pos(bucket=2, sub_type="earnings_spread",
                                     dte_remaining=20, profit_pct=10))
        assert rec.action == FULL_EXIT
        assert rec.rule_triggered == "b2b_21dte"

    def test_never_rolls(self, engine):
        # Across many states, Bucket 2B must never recommend ROLL.
        for dte in (5, 10, 20, 30, 60):
            for profit in (-250, -50, 0, 30, 60):
                rec = engine.check_exit(_pos(bucket=2, sub_type="iron_condor",
                                             dte_remaining=dte, profit_pct=profit))
                assert rec.action != ROLL

    def test_stop_loss_not_hold(self, engine):
        rec = engine.check_exit(_pos(bucket=2, sub_type="put_spread",
                                     dte_remaining=40, profit_pct=-220))
        assert rec.action == STOP_LOSS
        assert rec.action != HOLD


# ── Bucket 3 — LEAP / event ──────────────────────────────────────────────────
class TestBucket3:
    def test_40pct_profit_triggers_llm_review(self, engine):
        rec = engine.check_exit(_pos(bucket=3, sub_type="leap",
                                     dte_remaining=200, original_dte=300, profit_pct=45))
        assert rec.action == LLM_REVIEW

    def test_thesis_complete_full_exit(self, engine):
        rec = engine.check_exit(_pos(bucket=3, sub_type="event_call",
                                     dte_remaining=60, profit_pct=15,
                                     thesis_status="complete"))
        assert rec.action == FULL_EXIT
        assert rec.urgency == IMMEDIATE

    def test_dte_used_50pct_triggers_review(self, engine):
        rec = engine.check_exit(_pos(bucket=3, sub_type="leap",
                                     dte_remaining=100, original_dte=300, profit_pct=5))
        # 200/300 used = 66% → review
        assert rec.action == LLM_REVIEW
        assert rec.rule_triggered == "b3_dte_used_review"

    def test_full_loss_stop(self, engine):
        rec = engine.check_exit(_pos(bucket=3, sub_type="event_put",
                                     dte_remaining=10, original_dte=60, profit_pct=-100))
        assert rec.action == STOP_LOSS

    def test_never_rolls(self, engine):
        for profit in (-100, -20, 0, 45, 80):
            rec = engine.check_exit(_pos(bucket=3, sub_type="leap",
                                         dte_remaining=150, original_dte=300, profit_pct=profit))
            assert rec.action != ROLL

    def test_healthy_leap_holds(self, engine):
        rec = engine.check_exit(_pos(bucket=3, sub_type="leap",
                                     dte_remaining=280, original_dte=300, profit_pct=10))
        assert rec.action == HOLD


def test_unknown_bucket_returns_none(engine):
    assert engine.check_exit(_pos(bucket=9)) is None
