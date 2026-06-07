"""Regime tracker — fires only on regime transitions (Day 4).

Wraps SignalFusion and provides the alerting hook. Per CLAUDE.md Section 19
rule 1: alerts fire ONLY when the regime changes, never on every poll.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from core.logger import get_logger
from signals.signal_fusion import SignalFusion
from signals.signal_schema import MarketState, Signal

logger = get_logger(__name__)


class MarketStateTracker:
    """Feeds signals into SignalFusion and calls ``on_transition`` only when
    the regime actually changes.

    Usage:
        tracker = MarketStateTracker(on_transition=alert_engine.send_regime_alert)
        tracker.update(signals)
    """

    def __init__(
        self,
        on_transition: Optional[Callable[[MarketState], None]] = None,
    ) -> None:
        self._fusion = SignalFusion()
        self._on_transition = on_transition

    def update(self, signals: list[Signal]) -> MarketState:
        """Process signals and optionally fire the transition callback.

        Args:
            signals: Fresh signals from all active sources.

        Returns:
            The new MarketState (regardless of whether regime changed).
        """
        state = self._fusion.fuse(signals)

        if state.regime_changed and self._on_transition is not None:
            logger.info(
                "MarketStateTracker: transition %s → %s — firing callback",
                state.previous_regime,
                state.current_regime,
            )
            try:
                self._on_transition(state)
            except Exception as exc:
                logger.error("Transition callback failed: %s", exc)

        return state

    @property
    def current_state(self) -> Optional[MarketState]:
        return self._fusion.current_state


__all__ = ["MarketStateTracker"]
