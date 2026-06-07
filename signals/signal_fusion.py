"""Weighted signal fusion + MarketState transitions (Day 4).

Combines signals from macro, yield, sentiment, premarket, and technical
sources into a single composite MarketState. Alerts fire only on regime
CHANGE — never on every poll (CLAUDE.md Section 19, rule 1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger
from signals.signal_schema import Direction, MarketState, Regime, Signal

logger = get_logger(__name__)

# Direction → numeric score for weighted averaging
_DIR_SCORE: dict[Direction, int] = {
    "strong_short": -2,
    "short": -1,
    "neutral": 0,
    "long": 1,
    "strong_long": 2,
}

# Reverse map: composite score threshold → regime
def _score_to_regime(composite: int) -> Regime:
    if composite >= 75:
        return "strong_long"
    elif composite >= 55:
        return "long"
    elif composite <= 25:
        return "strong_short"
    elif composite <= 45:
        return "short"
    return "neutral"


class SignalFusion:
    """Fuses multiple source signals into a composite MarketState.

    Weights are read from settings (WEIGHT_MACRO, WEIGHT_YIELD, etc.).
    The WEIGHT_TF_ALIGNMENT_BONUS is an additive bonus applied when the
    short + mid + long timeframe signals all agree on the same direction.
    """

    def __init__(self) -> None:
        self._current_state: Optional[MarketState] = None

    def _weighted_score(self, signals: list[Signal]) -> int:
        """Return a composite 0-100 score from weighted signal directions."""
        weight_map = {
            "macro": settings.WEIGHT_MACRO,
            "yield": settings.WEIGHT_YIELD,
            "sentiment": settings.WEIGHT_SENTIMENT,
            "premarket": settings.WEIGHT_PREMARKET,
            "technical": settings.WEIGHT_TECHNICAL,
            "fusion": 0,  # fusion signals don't self-feed
        }

        total_weight = 0
        weighted_dir_sum = 0.0

        for sig in signals:
            w = weight_map.get(sig.source, 0)
            if w == 0:
                continue
            # confidence modulates the direction score
            dir_score = _DIR_SCORE.get(sig.direction, 0)
            # Blend: direction * (confidence/100)
            weighted_dir_sum += dir_score * (sig.confidence / 100.0) * w
            total_weight += w

        if total_weight == 0:
            return 50  # neutral default

        # Normalise to [-1, +1] range then map to [0, 100]
        normalised = weighted_dir_sum / (total_weight * 2)  # dir_score max=2
        score = int(50 + normalised * 50)
        return max(0, min(100, score))

    def _tf_alignment_bonus(self, signals: list[Signal]) -> int:
        """Return alignment bonus if all signals agree on direction."""
        if len(signals) < 2:
            return 0
        directions = {sig.direction for sig in signals}
        all_bullish = directions <= {"long", "strong_long"}
        all_bearish = directions <= {"short", "strong_short"}
        if all_bullish or all_bearish:
            return settings.WEIGHT_TF_ALIGNMENT_BONUS
        return 0

    def fuse(self, signals: list[Signal]) -> MarketState:
        """Combine signals into a new MarketState.

        Fires a regime-change log only when the regime actually changes.

        Args:
            signals: List of signals from any sources.

        Returns:
            Updated MarketState. The ``previous_regime`` field reflects the
            prior state if it has changed.
        """
        composite = self._weighted_score(signals)
        bonus = self._tf_alignment_bonus(signals)
        composite_with_bonus = max(0, min(100, composite + bonus))

        new_regime = _score_to_regime(composite_with_bonus)
        prev_regime = self._current_state.current_regime if self._current_state else None

        new_state = MarketState(
            current_regime=new_regime,
            composite_score=composite_with_bonus,
            last_updated=datetime.now(tz=timezone.utc),
            signals_active=signals,
            previous_regime=prev_regime,
        )

        if prev_regime is None:
            logger.info(
                "SignalFusion: initial regime=%s composite=%d",
                new_regime, composite_with_bonus,
            )
        elif new_state.regime_changed:
            logger.warning(
                "SignalFusion: REGIME CHANGE %s → %s (composite=%d)",
                prev_regime, new_regime, composite_with_bonus,
            )
        else:
            logger.debug(
                "SignalFusion: regime=%s composite=%d (no change)",
                new_regime, composite_with_bonus,
            )

        self._current_state = new_state
        return new_state

    @property
    def current_state(self) -> Optional[MarketState]:
        return self._current_state


__all__ = ["SignalFusion", "_score_to_regime"]
