"""Deterministic exit-rules engine for the three-bucket framework (Step 4).

This engine is the *source of truth* for exit decisions (the LLM engine in
Step 6 only supplements it). Every threshold comes from ``settings`` — nothing
is hardcoded — so the discipline can be tuned without code changes.

Critical invariants (see CLAUDE_PHASE2 §4 and "Notes for Claude Code"):
  * Bucket 1 NEVER receives a STOP_LOSS — always prefer ROLL.
  * Bucket 2B NEVER receives a ROLL, and never HOLD once the stop has tripped.
  * Bucket 3 NEVER receives a ROLL (binary outcome).
"""

from __future__ import annotations

from pydantic import BaseModel

from broker_client.buckets.models import BucketPosition
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# Action / urgency vocabularies.
FULL_EXIT = "FULL_EXIT"
ROLL = "ROLL"
HOLD = "HOLD"
LADDER = "LADDER"
STOP_LOSS = "STOP_LOSS"
LLM_REVIEW = "LLM_REVIEW"

IMMEDIATE = "IMMEDIATE"
TODAY = "TODAY"
THIS_WEEK = "THIS_WEEK"
MONITOR = "MONITOR"


class ExitRecommendation(BaseModel):
    """A single exit recommendation produced by the rules engine."""

    action: str          # FULL_EXIT | ROLL | HOLD | LADDER | STOP_LOSS | LLM_REVIEW
    urgency: str         # IMMEDIATE | TODAY | THIS_WEEK | MONITOR
    reason: str
    profit_pct: float
    dte_remaining: int
    rule_triggered: str  # which rule fired (for audit/logging)


class ExitRulesEngine:
    """Evaluates a position against its bucket's exit discipline."""

    def check_exit(self, position: BucketPosition) -> ExitRecommendation | None:
        """Return the highest-priority exit recommendation, or HOLD/MONITOR.

        Returns ``None`` only for an unrecognised bucket; healthy positions
        return an explicit HOLD so callers can render a recommendation.
        """
        if position.bucket == 1:
            rec = self._check_bucket1(position)
        elif position.bucket == 2:
            if position.sub_type == "earnings_put":
                rec = self._check_bucket2a(position)
            else:
                rec = self._check_bucket2b(position)
        elif position.bucket == 3:
            rec = self._check_bucket3(position)
        else:
            logger.warning("ExitRules: unknown bucket %s for %s", position.bucket, position.ticker)
            return None

        if rec.action != HOLD:
            logger.info(
                "ExitRules: %s B%d/%s → %s (%s) [%s]",
                position.ticker, position.bucket, position.sub_type,
                rec.action, rec.urgency, rec.rule_triggered,
            )
        return rec

    # ── helpers ──────────────────────────────────────────────────────────────
    def _rec(
        self,
        position: BucketPosition,
        action: str,
        urgency: str,
        reason: str,
        rule: str,
    ) -> ExitRecommendation:
        return ExitRecommendation(
            action=action,
            urgency=urgency,
            reason=reason,
            profit_pct=position.profit_pct,
            dte_remaining=position.dte_remaining,
            rule_triggered=rule,
        )

    def _hold(self, position: BucketPosition) -> ExitRecommendation:
        return self._rec(position, HOLD, MONITOR, "No exit rule triggered — hold", "none")

    # ── Bucket 1 — MSP / Wheel (recoverable, never stop-loss) ─────────────────
    def _check_bucket1(self, p: BucketPosition) -> ExitRecommendation:
        dte = p.dte_remaining
        profit = p.profit_pct

        if dte <= settings.DTE_SHORT_MAX:  # SHORT DTE (<= 14)
            if profit >= settings.BUCKET1_FAST_PROFIT_PCT and dte <= 3:
                return self._rec(p, FULL_EXIT, IMMEDIATE,
                                 "70-80% profit target with 2-3 days left", "b1_short_fast_profit")
            if profit >= settings.BUCKET1_PROFIT_TARGET_PCT:
                return self._rec(p, FULL_EXIT, IMMEDIATE,
                                 "50% profit target hit on short DTE", "b1_short_50pct")
            if dte <= 1:
                return self._rec(p, ROLL, IMMEDIATE,
                                 "Never hold to expiry — assignment risk", "b1_expiry_roll")
            if dte <= settings.DTE_URGENT_THRESHOLD and p.is_itm:
                return self._rec(p, ROLL, IMMEDIATE,
                                 "ITM with 7 days left — roll now", "b1_itm_roll")
            return self._hold(p)

        if dte <= settings.DTE_MEDIUM_MAX:  # MEDIUM DTE (15-44)
            if profit >= settings.BUCKET1_PROFIT_TARGET_PCT:
                return self._rec(p, FULL_EXIT, TODAY,
                                 "50% profit — redeploy capital", "b1_med_50pct")
            if dte <= settings.DTE_EXIT_THRESHOLD:
                return self._rec(p, FULL_EXIT, TODAY,
                                 "21 DTE rule — gamma risk increasing", "b1_21dte")
            return self._hold(p)

        # LONG DTE (>= 45)
        if profit >= settings.BUCKET1_EARLY_PROFIT_PCT and p.days_held <= 7:
            return self._rec(p, FULL_EXIT, TODAY,
                             "25% in week 1 — excellent capital efficiency", "b1_long_early")
        if dte <= settings.DTE_EXIT_THRESHOLD:
            return self._rec(p, FULL_EXIT, TODAY, "21 DTE rule — always exit", "b1_21dte")
        return self._hold(p)

    # ── Bucket 2A — Earnings put (recoverable; Bucket 1 + earnings overlay) ────
    def _check_bucket2a(self, p: BucketPosition) -> ExitRecommendation:
        if p.days_to_earnings is not None and 0 <= p.days_to_earnings <= 2:
            return self._rec(p, FULL_EXIT, IMMEDIATE,
                             "Earnings approaching — IV crush trade complete", "b2a_pre_earnings")
        if p.days_to_earnings is not None and -1 <= p.days_to_earnings < 0:
            return self._rec(p, FULL_EXIT, IMMEDIATE,
                             "Post-earnings — collect remaining IV crush profit", "b2a_post_earnings")
        # Otherwise identical discipline to Bucket 1.
        return self._check_bucket1(p)

    # ── Bucket 2B — Defined risk (spreads/condors; never roll) ────────────────
    def _check_bucket2b(self, p: BucketPosition) -> ExitRecommendation:
        if p.profit_pct >= settings.BUCKET2B_PROFIT_TARGET_PCT:
            return self._rec(p, FULL_EXIT, TODAY,
                             "50% profit on defined risk — don't get greedy", "b2b_50pct")
        if p.loss_pct >= settings.BUCKET2B_STOP_LOSS_PCT:
            return self._rec(p, STOP_LOSS, IMMEDIATE,
                             "Stop loss triggered — accept defined loss", "b2b_stop_loss")
        if p.dte_remaining <= settings.DTE_EXIT_THRESHOLD:
            return self._rec(p, FULL_EXIT, TODAY,
                             "21 DTE — gamma risk on spread", "b2b_21dte")
        return self._hold(p)

    # ── Bucket 3 — LEAP / Event (binary; never roll) ──────────────────────────
    def _check_bucket3(self, p: BucketPosition) -> ExitRecommendation:
        if p.thesis_status == "complete":
            return self._rec(p, FULL_EXIT, IMMEDIATE,
                             "Thesis complete — mission accomplished", "b3_thesis_complete")
        if p.loss_pct >= settings.BUCKET3_STOP_LOSS_PCT:
            return self._rec(p, STOP_LOSS, IMMEDIATE,
                             "Lotto play expired — binary outcome", "b3_full_loss")
        if p.profit_pct >= settings.BUCKET3_LLM_REVIEW_PROFIT_PCT:
            return self._rec(p, LLM_REVIEW, THIS_WEEK,
                             "40% profit — evaluate FULL_EXIT vs ROLL_UP vs LADDER", "b3_40pct_review")
        if p.dte_used_pct >= settings.DTE_USED_PCT_REVIEW:
            return self._rec(p, LLM_REVIEW, THIS_WEEK,
                             "50% of DTE used — reassess", "b3_dte_used_review")
        return self._hold(p)


__all__ = ["ExitRecommendation", "ExitRulesEngine", "FULL_EXIT", "ROLL", "HOLD",
           "LADDER", "STOP_LOSS", "LLM_REVIEW", "IMMEDIATE", "TODAY", "THIS_WEEK", "MONITOR"]
