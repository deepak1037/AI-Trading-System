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
        # Source instances are cached so repeated ticks don't re-init heavy
        # models (FinBERT loads ONCE on the cached SentimentScorer, not per tick).
        self._sources: dict[str, object] = {}
        self._sentiment_skip_logged = False
        self._sentiment_disabled = False  # set if FinBERT fails to load (torch, etc.)

    # Phases (ET) during which pre-market futures/crypto are still meaningful:
    # 7:00–8:15 (premarket) + 8:15–9:30 (macro) = pre-open. Skipped once the
    # market opens (session/power_hour) since intraday prices supersede futures.
    _PREMARKET_PHASES = frozenset({"premarket", "macro"})

    def _ensure_sentiment(self) -> Optional[object]:
        """Return a cached SentimentScorer, or None when sentiment is unavailable.

        Gated on NEWS_API_KEY (rule): without it we skip sentiment entirely
        rather than loading the heavy FinBERT model for RSS-only headlines.
        NEVER raises — if FinBERT fails to load (e.g. torch too old), sentiment
        is disabled for the process and the tick continues without it.
        """
        if self._sentiment_disabled:
            return None
        if not settings.NEWS_API_KEY:
            if not self._sentiment_skip_logged:
                logger.info("Sentiment skipped — no NEWS_API_KEY")
                self._sentiment_skip_logged = True
            return None
        scorer = self._sources.get("sentiment")
        if scorer is None:
            try:
                from signals.sentiment_scorer import SentimentScorer
                scorer = SentimentScorer()
                scorer.ensure_loaded()  # type: ignore[attr-defined]  # load FinBERT once
                self._sources["sentiment"] = scorer
            except Exception as exc:  # noqa: BLE001 — never fatal
                logger.warning(
                    "Sentiment disabled — FinBERT failed to load: %s "
                    "(check torch version: transformers needs torch>=2.4)", exc,
                )
                self._sentiment_disabled = True
                return None
        return scorer

    def warmup(self) -> None:
        """Pre-load heavy models at startup (FinBERT). Best-effort — never fatal."""
        try:
            self._ensure_sentiment()
        except Exception as exc:  # noqa: BLE001 — must not crash startup
            logger.warning("SignalFusion.warmup failed (non-fatal): %s", exc)

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

    def collect_signals(
        self, benchmark: str = "SPY", phase: Optional[str] = None
    ) -> list[Signal]:
        """Collect best-effort signals from the routinely-pollable sources.

        Each source is independently guarded — one that fails (missing API key,
        no data, model not downloaded) is skipped rather than aborting the tick.

        - Technical (yfinance): reliable anchor, always polled.
        - Treasury-yield delta: added when FRED is configured.
        - Sentiment (FinBERT): polled only when NEWS_API_KEY is set; model is
          loaded once and cached.
        - Pre-market futures/crypto: polled only in the pre-open phases
          (``premarket``/``macro``); skipped during the session (stale).
        - Macro (NFP/CPI): event-driven, fired on release — not polled here.
        """
        signals: list[Signal] = []

        # ── Technical (reliable, no credentials) ─────────────────────────────
        try:
            from signals.technical_module import TechnicalModule
            tech = self._sources.get("technical")
            if tech is None:
                tech = TechnicalModule()
                self._sources["technical"] = tech
            signals.append(tech.score_ticker(benchmark))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("collect_signals: technical failed: %s", exc)

        # ── Treasury-yield delta (continuous read; needs FRED/yfinance) ──────
        try:
            from signals.yield_monitor import YieldMonitor
            ym = self._sources.get("yield")
            if ym is None:
                ym = YieldMonitor()
                self._sources["yield"] = ym
            ysig = ym.read()  # type: ignore[attr-defined]  # sets baseline + always returns a reading
            if ysig is not None:
                signals.append(ysig)
        except Exception as exc:  # noqa: BLE001
            logger.debug("collect_signals: yield failed: %s", exc)

        # ── Sentiment (FinBERT; gated on NEWS_API_KEY, model cached) ──────────
        try:
            scorer = self._ensure_sentiment()
            if scorer is not None:
                signals.append(scorer.score())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("collect_signals: sentiment failed: %s", exc)

        # ── Pre-market futures/crypto (only pre-open phases) ──────────────────
        if phase in self._PREMARKET_PHASES:
            try:
                from signals.premarket_watcher import PremarketWatcher
                pw = self._sources.get("premarket")
                if pw is None:
                    pw = PremarketWatcher()
                    self._sources["premarket"] = pw
                signals.append(pw.check())  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.debug("collect_signals: premarket failed: %s", exc)

        logger.debug("collect_signals: gathered %d signal(s)", len(signals))
        return signals

    def compute(self, benchmark: str = "SPY", phase: Optional[str] = None) -> MarketState:
        """Collect signals from available sources and fuse them into a MarketState.

        Convenience entry point for the watcher tick:
        ``SignalFusion().compute(phase=phase)``.
        """
        return self.fuse(self.collect_signals(benchmark, phase=phase))

    @property
    def current_state(self) -> Optional[MarketState]:
        return self._current_state


__all__ = ["SignalFusion", "_score_to_regime"]
