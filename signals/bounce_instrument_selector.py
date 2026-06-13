"""Bounce instrument selector (Phase 3, Module B, Step 9).

Given a ``BounceScore`` and the drop's cause, choose the concrete Bucket-3
instrument and sizing:

  * HIGH-confidence PURE_SENTIMENT → slightly-OTM short-dated CALL (leverage on a
    fast sentiment-clearing bounce).
  * HIGH-confidence HYBRID in a market crash → deep-ITM LEAP (acts like stock,
    long horizon to let the recovery play out).
  * MEDIUM confidence → defined-risk CALL SPREAD (caps cost and upside).
  * LOW confidence → no trade (None).

All deltas, expiries, targets, and the Bucket-3 sizing cap come from ``settings``.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger
from signals.bounce_scorer import BounceScore
from signals.drop_classifier import HYBRID, PURE_SENTIMENT

logger = get_logger(__name__)


class BounceTradeSetup(BaseModel):
    """A concrete Bucket-3 bounce trade setup."""

    ticker: str
    bucket: int = 3
    sub_type: str = "event_call"        # event_call | leap | event_stock

    instrument: str = "call"            # call | stock | call_spread
    strike: float | None = None
    expiry: date | None = None
    delta: float | None = None          # target delta for calls

    max_capital: float = 0.0            # BUCKET3_MAX_SINGLE_TRADE_PCT * portfolio
    contracts: int | None = None
    shares: int | None = None

    profit_target_pct: float = 0.0
    stop_loss_pct: float | None = None  # for stocks only
    thesis_complete_signal: str = ""

    entry_urgency: str = "TODAY"        # IMMEDIATE | TODAY | THIS_WEEK
    hold_period: str = "weeks"          # days | weeks | months


class InstrumentSelector:
    """Selects the Bucket-3 instrument for a bounce play."""

    def select(
        self,
        bounce_score: BounceScore,
        drop_cause: str | None = None,
        regime: str | None = None,
        portfolio_value: float = 0.0,
    ) -> BounceTradeSetup | None:
        cause = drop_cause or bounce_score.classification
        score = bounce_score.overall_score
        max_capital = round(
            portfolio_value * settings.BUCKET3_MAX_SINGLE_TRADE_PCT / 100.0, 2
        )
        strong = settings.BOUNCE_STRONG_BUY_SCORE

        # HIGH confidence + pure sentiment → leveraged short-dated call.
        if score >= strong and cause == PURE_SENTIMENT:
            return BounceTradeSetup(
                ticker=bounce_score.ticker,
                sub_type="event_call",
                instrument="call",
                delta=settings.BOUNCE_CALL_DELTA,
                expiry=self._weeks_out(settings.BOUNCE_CALL_EXPIRY_WEEKS),
                profit_target_pct=settings.BOUNCE_CALL_PROFIT_TARGET_PCT,
                max_capital=max_capital,
                entry_urgency="IMMEDIATE",
                hold_period="days",
                thesis_complete_signal="Sentiment clears / price reclaims pre-drop level",
            )

        # HIGH confidence + hybrid in a crash → deep-ITM LEAP (stock-like).
        if score >= strong and cause == HYBRID and regime == "crash":
            return BounceTradeSetup(
                ticker=bounce_score.ticker,
                sub_type="leap",
                instrument="call",
                delta=settings.BOUNCE_LEAP_DELTA,
                expiry=self._months_out(settings.BOUNCE_LEAP_EXPIRY_MONTHS),
                profit_target_pct=settings.BOUNCE_LEAP_PROFIT_TARGET_PCT,
                max_capital=max_capital,
                entry_urgency="TODAY",
                hold_period="months",
                thesis_complete_signal="Market recovers / one-time impact lapped",
            )

        # MEDIUM confidence → defined-risk call spread.
        if score >= settings.DROP_BOUNCE_MIN_SCORE:
            return BounceTradeSetup(
                ticker=bounce_score.ticker,
                sub_type="event_call",
                instrument="call_spread",
                expiry=self._weeks_out(settings.BOUNCE_CALL_EXPIRY_WEEKS),
                profit_target_pct=settings.BOUNCE_SPREAD_PROFIT_TARGET_PCT,
                max_capital=max_capital,
                entry_urgency="TODAY",
                hold_period="weeks",
                thesis_complete_signal="Bounce target hit or thesis invalidated",
            )

        # LOW confidence → no trade.
        logger.info(
            "InstrumentSelector: %s score %d below trade threshold — no setup",
            bounce_score.ticker, score,
        )
        return None

    # ── expiry helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _weeks_out(weeks: int) -> date:
        target = date.today() + timedelta(weeks=weeks)
        days_to_friday = (4 - target.weekday()) % 7
        return target + timedelta(days=days_to_friday)

    @staticmethod
    def _months_out(months: int) -> date:
        return date.today() + timedelta(days=int(months * 30.4))


__all__ = ["BounceTradeSetup", "InstrumentSelector"]
