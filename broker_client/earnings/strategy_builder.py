"""Earnings strategy builder (Phase 3, Step 3).

Turns an ``IVAnalysis`` verdict into a concrete trade:

  * ``IV_CRUSH`` → sell an OTM put (recoverable, Bucket 2A) whose strike clears
    the historically-safe move. When a broker is supplied, the real premium and
    Schwab buying-power margin come from ``LLMROIAnalyzer.build_put_roi`` (the
    same previewOrder path ``make analyze`` uses); otherwise a transparent
    estimate is used.

  * ``IV_SPIKE`` → buy an ATM straddle entered ~7 days out, with a hard
    ``exit_date`` one day BEFORE earnings. The ``IVSpikeTrade`` model itself
    refuses to be built holding through earnings (Phase 3 note 3).

Strike selection blends the expected move, the historically-safe move level,
and an extra safety buffer — all from ``settings``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    EarningsEvent,
    IVAnalysis,
    IVCrushTrade,
    IVSpikeTrade,
)
from config.settings import settings
from core.logger import get_logger

if TYPE_CHECKING:
    from broker_core.base_broker import BaseBroker
    from broker_client.llm_roi_analyzer import LLMROIAnalyzer

logger = get_logger(__name__)

_MULT = settings.OPTIONS_CONTRACT_MULTIPLIER


class EarningsStrategyBuilder:
    """Builds IV-crush / IV-spike trades from an IV analysis."""

    def __init__(
        self,
        broker: BaseBroker | None = None,
        roi_analyzer: LLMROIAnalyzer | None = None,
    ) -> None:
        self._broker = broker
        self._roi_analyzer = roi_analyzer  # injected for tests; built lazily otherwise

    # ── dispatch ───────────────────────────────────────────────────────────────
    def build(
        self, event: EarningsEvent, analysis: IVAnalysis
    ) -> IVCrushTrade | IVSpikeTrade | None:
        """Build the trade matching the analysis verdict (None for SKIP)."""
        if analysis.strategy == IV_CRUSH:
            return self.build_iv_crush_trade(event, analysis)
        if analysis.strategy == IV_SPIKE:
            return self.build_iv_spike_trade(event, analysis)
        return None

    # ── IV crush (sell premium) ────────────────────────────────────────────────
    def build_iv_crush_trade(
        self, event: EarningsEvent, analysis: IVAnalysis
    ) -> IVCrushTrade | None:
        price = self._current_price(event)
        if price is None or price <= 0:
            logger.warning("No price for %s — cannot build IV crush trade", event.ticker)
            return None

        expected_move = analysis.expected_move_current or event.expected_move
        safe_level = analysis.safe_move_level or expected_move
        put_strike = self.select_put_strike(price, expected_move, safe_level)
        if put_strike <= 0:
            return None
        put_expiry = self._expiry_after_earnings(event)
        put_safety_pct = round((price - put_strike) / price * 100.0, 2) if price else 0.0

        premium, margin, margin_basis = self._price_short_put(
            event.ticker, put_strike, put_expiry, price, expected_move
        )
        premium_per_share = premium / _MULT
        breakeven = put_strike - premium_per_share
        roi_margin = round(premium / margin, 4) if margin > 0 else 0.0
        cs_margin = put_strike * _MULT
        roi_cs = round(premium / cs_margin, 4) if cs_margin > 0 else 0.0
        break_even_pct = round((price - breakeven) / price * 100.0, 2) if price else 0.0

        trade = IVCrushTrade(
            ticker=event.ticker,
            earnings_date=event.earnings_date,
            earnings_time=event.earnings_time,
            put_strike=put_strike,
            put_expiry=put_expiry,
            put_premium=round(premium, 2),
            put_safety_pct=put_safety_pct,
            total_premium=round(premium, 2),
            margin_required=round(margin, 2),
            margin_basis=margin_basis,
            roi_margin=roi_margin,
            roi_cash_secured=roi_cs,
            expected_move=round(expected_move, 2),
            put_breach_probability=round(analysis.breach_rate_lower, 3),
            break_even_pct=break_even_pct,
        )
        logger.info(
            "IV_CRUSH %s: sell %.0fP @ $%.0f, margin $%.0f (%s), ROI %.1f%%",
            event.ticker, put_strike, premium, margin, margin_basis, roi_margin * 100,
        )
        return trade

    @staticmethod
    def select_put_strike(
        current_price: float,
        expected_move: float,
        safe_move_level: float,
        safety_buffer: float | None = None,
    ) -> float:
        """Strike that clears max(expected, historically-safe) move + a buffer.

        Rounded to the nearest standard increment (``EARNINGS_STRIKE_ROUND``).
        """
        buffer = safety_buffer if safety_buffer is not None else settings.EARNINGS_SAFETY_BUFFER
        safe_distance = max(expected_move, safe_move_level) * buffer
        raw_strike = current_price * (1 - safe_distance / 100.0)
        step = settings.EARNINGS_STRIKE_ROUND
        if step <= 0:
            return round(raw_strike, 2)
        return round(raw_strike / step) * step

    def _price_short_put(
        self,
        ticker: str,
        strike: float,
        expiry: date,
        price: float,
        expected_move: float,
    ) -> tuple[float, float, str]:
        """(premium_per_contract, margin_per_contract, basis).

        Prefers the real Schwab path (LLMROIAnalyzer.build_put_roi with broker
        preview margin); falls back to a transparent estimate + Reg-T margin.
        """
        if self._broker is not None:
            try:
                analyzer = self._ensure_roi_analyzer()
                roi = analyzer.build_put_roi(
                    ticker, strike, expiry.isoformat(), use_broker_margin=True
                )
                return roi.premium_per_contract, roi.margin_per_contract, roi.margin_basis
            except Exception as exc:  # noqa: BLE001 — degrade to estimate
                logger.debug("Real put pricing failed for %s: %s — estimating", ticker, exc)
        premium = self._estimate_put_premium(price, strike, expected_move)
        margin = self._reg_t_margin(strike, price, premium / _MULT)
        return premium, margin, "estimate"

    def _ensure_roi_analyzer(self) -> LLMROIAnalyzer:
        if self._roi_analyzer is None:
            from broker_client.llm_roi_analyzer import LLMROIAnalyzer

            self._roi_analyzer = LLMROIAnalyzer(broker=self._broker)
        return self._roi_analyzer

    @staticmethod
    def _estimate_put_premium(price: float, strike: float, expected_move: float) -> float:
        """Rough OTM short-put premium (per contract) when no chain is available.

        The expected move approximates the ATM straddle, so one ATM leg ≈ half of
        it; premium decays linearly to ~0 by twice the expected move OTM.
        """
        em = max(expected_move, 0.1)
        atm_leg = price * (em / 100.0) * 0.5
        otm_pct = max((price - strike) / price * 100.0, 0.0)
        decay = max(0.0, 1.0 - otm_pct / (2.0 * em))
        per_share = max(atm_leg * decay, 0.05)
        return round(per_share * _MULT, 2)

    @staticmethod
    def _reg_t_margin(strike: float, underlying: float, premium_per_share: float) -> float:
        otm_amount = max(underlying - strike, 0.0)
        req = max(0.20 * underlying - otm_amount, 0.10 * strike)
        return round((req + premium_per_share) * _MULT, 2)

    # ── IV spike (buy premium, exit before earnings) ───────────────────────────
    def build_iv_spike_trade(
        self, event: EarningsEvent, analysis: IVAnalysis
    ) -> IVSpikeTrade | None:
        price = self._current_price(event)
        if price is None or price <= 0:
            logger.warning("No price for %s — cannot build IV spike trade", event.ticker)
            return None

        entry_date, exit_date = self._spike_window(event.earnings_date)
        if exit_date < date.today():
            logger.warning(
                "IV spike window already passed for %s (earnings %s) — skipping",
                event.ticker, event.earnings_date,
            )
            return None
        strike = self._round_strike(price)
        expiry = self._expiry_after_earnings(event)

        # ATM straddle: expected move ≈ straddle price, so total ≈ price * em%.
        em = analysis.expected_move_current or event.expected_move
        straddle_per_share = price * (em / 100.0) if em > 0 else price * 0.05
        total_premium = round(straddle_per_share * _MULT, 2)

        try:
            trade = IVSpikeTrade(
                ticker=event.ticker,
                earnings_date=event.earnings_date,
                entry_date=entry_date,
                exit_date=exit_date,
                instrument="straddle",
                call_strike=strike,
                put_strike=strike,
                expiry=expiry,
                total_premium_paid=total_premium,
                max_loss=total_premium,
                target_profit_pct=settings.EARNINGS_SPIKE_TARGET_PROFIT_PCT,
                iv_rank_at_entry=analysis.iv_rank_current,
                iv_expansion_expected=round(
                    max(settings.EARNINGS_IV_RANK_SELL_THRESHOLD - analysis.iv_rank_current, 0)
                    * 0.5, 1,
                ),
            )
        except ValueError as exc:
            # Earnings too close to build a valid pre-earnings exit window.
            logger.warning("Cannot build IV spike for %s: %s", event.ticker, exc)
            return None

        logger.info(
            "IV_SPIKE %s: buy %s @ $%.0f, exit %s (before %s), target +%.0f%%",
            event.ticker, trade.instrument, total_premium, exit_date,
            event.earnings_date, trade.target_profit_pct,
        )
        return trade

    @staticmethod
    def _spike_window(earnings_date: date) -> tuple[date, date]:
        """Entry ~7 days before earnings (not in the past); exit 1 day before."""
        exit_date = earnings_date - timedelta(days=1)
        entry_date = earnings_date - timedelta(days=7)
        today = date.today()
        if entry_date < today:
            entry_date = min(today, exit_date)
        return entry_date, exit_date

    # ── shared helpers ─────────────────────────────────────────────────────────
    def _current_price(self, event: EarningsEvent) -> float | None:
        if event.stock_price and event.stock_price > 0:
            return float(event.stock_price)
        if self._broker is not None:
            try:
                q = self._broker.get_quote(event.ticker)
                price = q.last or (
                    (q.bid + q.ask) / 2 if (q.bid and q.ask) else None
                )
                if price and price > 0:
                    return float(price)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Quote failed for %s: %s", event.ticker, exc)
        return None

    @staticmethod
    def _round_strike(price: float) -> float:
        step = settings.EARNINGS_STRIKE_ROUND
        if step <= 0:
            return round(price, 2)
        return round(price / step) * step

    @staticmethod
    def _expiry_after_earnings(event: EarningsEvent) -> date:
        """First Friday on/after earnings + 1-2 days (captures the IV crush)."""
        buffer = 1 if event.earnings_time == "BMO" else 2
        target = event.earnings_date + timedelta(days=buffer)
        # Weekly options expire Friday (weekday 4).
        days_to_friday = (4 - target.weekday()) % 7
        return target + timedelta(days=days_to_friday)


__all__ = ["EarningsStrategyBuilder"]
