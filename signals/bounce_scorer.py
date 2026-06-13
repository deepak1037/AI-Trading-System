"""Bounce scorer (Phase 3, Module B, Step 8).

Turns a ``DropClassification`` into a 0–100 ``BounceScore`` across five weighted
components (cause, fundamental, institutional, technical, timing) and a coarse
trade decision. Bounce plays are always Bucket 3 (binary lotto risk), so a
``FUNDAMENTAL`` drop can never score into a trade — the cause component caps it.

Weights and thresholds come from ``settings`` (BOUNCE_WEIGHT_* / DROP_*).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from config.settings import settings
from core.logger import get_logger
from signals.drop_classifier import (
    EARNINGS_MISS,
    FUNDAMENTAL,
    HYBRID,
    PURE_SENTIMENT,
    DropClassification,
)

logger = get_logger(__name__)


class BounceScore(BaseModel):
    """A scored bounce candidate with a coarse trade decision."""

    ticker: str
    overall_score: int = Field(default=0, ge=0, le=100)
    classification: str = HYBRID

    # Component scores (each 0-100).
    cause_score: int = 0
    fundamental_score: int = 0
    institutional_score: int = 0
    technical_score: int = 0
    timing_score: int = 0

    # Decision.
    trade_recommendation: str = "SKIP"   # STRONG_BUY | BUY | WAIT | SKIP
    suggested_bucket: int = 3            # always Bucket 3 for bounces
    suggested_instrument: str = "none"   # calls | stock | call_spread | none
    urgency: str = "SKIP"                # IMMEDIATE | TODAY | THIS_WEEK | SKIP


class BounceScorer:
    """Scores how tradeable a classified drop is as a Bucket-3 bounce."""

    def score(
        self,
        classification: DropClassification,
        rsi: float | None = None,
        regime: str | None = None,
    ) -> BounceScore:
        cause = self._score_cause(classification.classification)
        fundamental = self._score_fundamental(classification)
        institutional = self._score_institutional(classification.institutional_action)
        technical = self._score_technical(classification.drop_pct, rsi)
        timing = self._score_timing(classification.sector_peers, classification.drop_pct)

        overall = round(
            cause * settings.BOUNCE_WEIGHT_CAUSE
            + fundamental * settings.BOUNCE_WEIGHT_FUNDAMENTAL
            + institutional * settings.BOUNCE_WEIGHT_INSTITUTIONAL
            + technical * settings.BOUNCE_WEIGHT_TECHNICAL
            + timing * settings.BOUNCE_WEIGHT_TIMING
        )
        overall = int(max(0, min(overall, 100)))

        rec, instrument, urgency = self._decide(
            overall, classification.classification
        )
        result = BounceScore(
            ticker=classification.ticker,
            overall_score=overall,
            classification=classification.classification,
            cause_score=cause,
            fundamental_score=fundamental,
            institutional_score=institutional,
            technical_score=technical,
            timing_score=timing,
            trade_recommendation=rec,
            suggested_instrument=instrument,
            urgency=urgency,
        )
        logger.info(
            "BounceScore %s: %d/100 (%s) → %s [%s]",
            result.ticker, overall, classification.classification, rec, urgency,
        )
        return result

    # ── component scores ──────────────────────────────────────────────────────
    @staticmethod
    def _score_cause(classification: str) -> int:
        return {
            PURE_SENTIMENT: 90,   # clear, temporary cause → high conviction
            HYBRID: 70,           # real but one-time
            FUNDAMENTAL: 10,      # don't trade against it
            EARNINGS_MISS: 40,    # depends on details
        }.get(classification, 0)

    @staticmethod
    def _score_fundamental(classification: DropClassification) -> int:
        """Are the fundamentals still intact? Fewer damage signals → higher."""
        if classification.classification == FUNDAMENTAL:
            return 15
        n = len(classification.fundamental_signals)
        return max(100 - 25 * n, 25)

    @staticmethod
    def _score_institutional(action: str) -> int:
        return {"buying": 90, "selling": 20, "neutral": 50}.get(action, 50)

    @staticmethod
    def _score_technical(drop_pct: float, rsi: float | None) -> int:
        """Oversold proxy: explicit RSI when known, else the drop magnitude."""
        if rsi is not None:
            if rsi <= 30:
                return 90
            if rsi <= 40:
                return 70
            if rsi >= 70:
                return 20
            return 50
        # No RSI: a bigger drop is more likely oversold/mean-reverting.
        return int(max(30, min(40 + drop_pct * 6, 95)))

    @staticmethod
    def _score_timing(sector_peers: str, drop_pct: float) -> int:
        base = {
            "isolated": 75,        # company-specific = cleaner entry
            "partial_sector": 55,
            "sector_wide": 40,     # macro drag, wait for stabilization
        }.get(sector_peers, 50)
        # A very sharp single-day drop often marks a near-term capitulation.
        if drop_pct >= 2 * settings.DROP_ALERT_THRESHOLD_PCT:
            base = min(base + 10, 100)
        return base

    # ── decision ──────────────────────────────────────────────────────────────
    @staticmethod
    def _decide(overall: int, classification: str) -> tuple[str, str, str]:
        """(trade_recommendation, suggested_instrument, urgency)."""
        if classification == FUNDAMENTAL:
            return "SKIP", "none", "SKIP"
        if overall >= settings.BOUNCE_STRONG_BUY_SCORE:
            return "STRONG_BUY", "calls", "IMMEDIATE"
        if overall >= settings.DROP_BOUNCE_MIN_SCORE:
            return "BUY", "call_spread", "TODAY"
        if overall >= 40:
            return "WAIT", "none", "THIS_WEEK"
        return "SKIP", "none", "SKIP"


__all__ = ["BounceScore", "BounceScorer"]
