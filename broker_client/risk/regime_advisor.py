"""Macro regime → strategy adjustment advisor (Phase 4 Step 6).

Translates the current MarketState composite score into concrete guidance
for each bucket. Feeds into the daily briefing and pre-trade checker's
OTM buffer multiplier. Does NOT change signal weights — those are fixed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from core.logger import get_logger

logger = get_logger(__name__)


class RegimeAdvice(BaseModel):
    regime: str              # "bull" | "bear" | "volatile" | "choppy"
    confidence: int          # 0-100, from market state composite score

    bucket1_adjustment: str  # "aggressive" | "normal" | "conservative"
    bucket2_adjustment: str
    bucket3_adjustment: str

    guidance: list[str]
    warnings: list[str]

    put_otm_buffer_multiplier: float
    # 1.0 = normal, 1.2 = wider (more conservative), 0.9 = tighter


class RegimeAdvisor:
    """Maps composite_score → per-bucket strategy adjustments."""

    def advise(self, market_state: Any, positions: list[Any] | None = None) -> RegimeAdvice:
        """Return strategy adjustments for the current regime.

        market_state must expose .composite_score (0-100) and .current_regime.
        """
        composite = 50
        try:
            composite = int(getattr(market_state, "composite_score", 50) or 50)
        except Exception as exc:
            logger.debug("RegimeAdvisor: market_state read failed: %s", exc)

        if composite >= 65:
            return self._bullish(composite)
        if composite <= 35:
            return self._bearish(composite)
        return self._choppy(composite)

    # ── regime handlers ──────────────────────────────────────────────────────

    @staticmethod
    def _bullish(score: int) -> RegimeAdvice:
        return RegimeAdvice(
            regime="bull",
            confidence=score,
            bucket1_adjustment="aggressive",
            bucket2_adjustment="normal",
            bucket3_adjustment="aggressive",
            put_otm_buffer_multiplier=0.95,
            guidance=[
                "Bullish regime: put selling favorable",
                "Bounces recover quickly — Bucket 3 plays higher conviction",
                "Consider selling puts slightly closer to ATM for more premium",
            ],
            warnings=[],
        )

    @staticmethod
    def _bearish(score: int) -> RegimeAdvice:
        return RegimeAdvice(
            regime="bear",
            confidence=score,
            bucket1_adjustment="conservative",
            bucket2_adjustment="conservative",
            bucket3_adjustment="reduced",
            put_otm_buffer_multiplier=1.3,
            guidance=[
                "Bearish regime: increase put strike distance",
                "Reduce Bucket 3 position sizes",
                "Bounces may be dead cat bounces — require higher conviction",
                "Consider iron condors over naked puts",
            ],
            warnings=[
                "Market in bearish regime — all positions should be reviewed",
            ],
        )

    @staticmethod
    def _choppy(score: int) -> RegimeAdvice:
        return RegimeAdvice(
            regime="choppy",
            confidence=score,
            bucket1_adjustment="normal",
            bucket2_adjustment="normal",
            bucket3_adjustment="reduced",
            put_otm_buffer_multiplier=1.1,
            guidance=[
                "Choppy market: earnings plays most reliable strategy",
                "Avoid directional Bucket 3 plays",
                "Iron condors work well in range-bound market",
            ],
            warnings=[],
        )


__all__ = ["RegimeAdvisor", "RegimeAdvice"]
