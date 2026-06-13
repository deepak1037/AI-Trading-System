"""Tests for the Phase 3 IV-spike force-exit guard in the exit-rules engine."""

from __future__ import annotations

import pytest

from broker_client.buckets.exit_rules import FULL_EXIT, IMMEDIATE, ExitRulesEngine
from broker_client.buckets.models import BucketPosition


@pytest.fixture
def engine():
    return ExitRulesEngine()


def _spike(**kw) -> BucketPosition:
    base = {
        "ticker": "TSLA", "bucket": 2, "sub_type": "iv_spike",
        "strategy": "IV_SPIKE", "dte_remaining": 1, "profit_pct": -10.0,
    }
    base.update(kw)
    return BucketPosition(**base)


def test_iv_spike_force_exit_at_1_dte_even_at_a_loss(engine) -> None:
    rec = engine.check_exit(_spike(dte_remaining=1, profit_pct=-30.0))
    assert rec.action == FULL_EXIT
    assert rec.urgency == IMMEDIATE
    assert rec.rule_triggered == "iv_spike_force_exit"


def test_iv_spike_force_exit_overrides_profit(engine) -> None:
    # Even sitting in profit, an IV spike must not be held into earnings.
    rec = engine.check_exit(_spike(dte_remaining=0, profit_pct=120.0))
    assert rec.action == FULL_EXIT
    assert rec.rule_triggered == "iv_spike_force_exit"


def test_iv_spike_detected_via_strategy_only(engine) -> None:
    rec = engine.check_exit(_spike(sub_type="defined_risk", strategy="IV_SPIKE"))
    assert rec.rule_triggered == "iv_spike_force_exit"


def test_iv_spike_not_forced_when_dte_high(engine) -> None:
    # Plenty of DTE left → the force-exit does NOT fire; normal bucket rule applies.
    rec = engine.check_exit(_spike(dte_remaining=10, profit_pct=5.0))
    assert rec.rule_triggered != "iv_spike_force_exit"


def test_non_spike_not_affected(engine) -> None:
    pos = BucketPosition(
        ticker="AAPL", bucket=2, sub_type="defined_risk", strategy="iron_condor",
        dte_remaining=1, profit_pct=10.0,
    )
    rec = engine.check_exit(pos)
    assert rec.rule_triggered != "iv_spike_force_exit"
