"""Concentration alerts — sector, ticker, expiry, and cash buffer checks.

Called by the daily briefing and pre-trade checker to surface portfolio
concentration risks before they become real losses.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from broker_client.risk.portfolio_heat_map import PortfolioHeatMap
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class ConcentrationAlert(BaseModel):
    alert_type: str      # SECTOR | EXPIRY | TICKER | CASH
    severity: str        # WARNING | CRITICAL
    message: str
    current_value: float
    threshold: float
    affected: list[str]
    suggestion: str


def _get(pos: Any, key: str, default: Any = None) -> Any:
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


class ConcentrationChecker:
    """Checks open positions for concentration risks and returns alert list."""

    def __init__(self, paper_account: Any = None) -> None:
        self._paper = paper_account
        self._heat_map = PortfolioHeatMap(paper_account)

    def check(self, positions: list[Any], portfolio_value: float | None = None) -> list[ConcentrationAlert]:
        """Return all active concentration alerts for the position list."""
        if portfolio_value is None:
            portfolio_value = self._portfolio_value()

        total_exp = self._total_exposure(positions, portfolio_value)
        alerts: list[ConcentrationAlert] = []

        alerts.extend(self._sector_alerts(positions, total_exp))
        alerts.extend(self._ticker_alerts(positions, total_exp))
        alerts.extend(self._expiry_alerts(positions))
        alerts.extend(self._cash_alerts(portfolio_value))

        logger.debug(
            "ConcentrationChecker: %d position(s) → %d alert(s)",
            len(positions), len(alerts),
        )
        return alerts

    # ── sector ───────────────────────────────────────────────────────────────

    def _sector_alerts(self, positions: list[Any], total: float) -> list[ConcentrationAlert]:
        alerts = []
        report = self._heat_map.generate(positions)
        for sector, exp in report.sector_exposure.items():
            pct = exp.exposure_pct
            if pct > settings.SECTOR_CRITICAL_PCT:
                alerts.append(ConcentrationAlert(
                    alert_type="SECTOR",
                    severity="CRITICAL",
                    message=f"{sector} concentration at {pct:.1f}% (threshold: {settings.SECTOR_CRITICAL_PCT:.0f}%)",
                    current_value=pct,
                    threshold=settings.SECTOR_CRITICAL_PCT,
                    affected=exp.tickers,
                    suggestion=f"Reduce {sector} exposure immediately — concentration is critical",
                ))
            elif pct > settings.SECTOR_WARNING_PCT:
                alerts.append(ConcentrationAlert(
                    alert_type="SECTOR",
                    severity="WARNING",
                    message=f"{sector} concentration at {pct:.1f}% (threshold: {settings.SECTOR_WARNING_PCT:.0f}%)",
                    current_value=pct,
                    threshold=settings.SECTOR_WARNING_PCT,
                    affected=exp.tickers,
                    suggestion=f"Consider hedging {sector} exposure or avoid new {sector} positions",
                ))
        return alerts

    # ── ticker ───────────────────────────────────────────────────────────────

    def _ticker_alerts(self, positions: list[Any], total: float) -> list[ConcentrationAlert]:
        if total <= 0:
            return []
        alerts = []
        ticker_exp: dict[str, float] = {}
        for pos in positions:
            ticker = str(_get(pos, "ticker", "") or "")
            entry = abs(float(_get(pos, "entry_price", 0) or 0))
            qty = abs(int(_get(pos, "qty", 1) or 1))
            is_opt = _get(pos, "option_type") in ("put", "call")
            mult = settings.OPTIONS_CONTRACT_MULTIPLIER if is_opt else 1
            ticker_exp[ticker] = ticker_exp.get(ticker, 0.0) + entry * qty * mult / total * 100.0

        for ticker, pct in ticker_exp.items():
            if pct > settings.SINGLE_TICKER_WARNING_PCT:
                severity = "CRITICAL" if pct > settings.SINGLE_TICKER_WARNING_PCT * 1.5 else "WARNING"
                alerts.append(ConcentrationAlert(
                    alert_type="TICKER",
                    severity=severity,
                    message=f"{ticker} represents {pct:.1f}% of portfolio (threshold: {settings.SINGLE_TICKER_WARNING_PCT:.0f}%)",
                    current_value=pct,
                    threshold=settings.SINGLE_TICKER_WARNING_PCT,
                    affected=[ticker],
                    suggestion=f"Reduce {ticker} position or avoid adding more exposure",
                ))
        return alerts

    # ── expiry ───────────────────────────────────────────────────────────────

    def _expiry_alerts(self, positions: list[Any]) -> list[ConcentrationAlert]:
        today = date.today()
        week_buckets: dict[str, list[str]] = {}
        for pos in positions:
            expiry_str = _get(pos, "expiry")
            if not expiry_str:
                continue
            try:
                exp = date.fromisoformat(str(expiry_str))
            except (ValueError, TypeError):
                continue
            days = (exp - today).days
            if days < 0:
                continue
            # Bucket by ISO week
            week_key = exp.strftime("%Y-W%W")
            ticker = str(_get(pos, "ticker", "") or "unknown")
            week_buckets.setdefault(week_key, []).append(ticker)

        alerts = []
        for week, tickers in week_buckets.items():
            count = len(tickers)
            if count >= settings.SAME_EXPIRY_WARNING:
                alerts.append(ConcentrationAlert(
                    alert_type="EXPIRY",
                    severity="WARNING",
                    message=f"{count} positions expire week of {week} (threshold: {settings.SAME_EXPIRY_WARNING})",
                    current_value=float(count),
                    threshold=float(settings.SAME_EXPIRY_WARNING),
                    affected=tickers,
                    suggestion="Stagger expiry dates to reduce gamma risk concentration",
                ))
        return alerts

    # ── cash ─────────────────────────────────────────────────────────────────

    def _cash_alerts(self, portfolio_value: float) -> list[ConcentrationAlert]:
        cash_pct = self._cash_pct(portfolio_value)
        if cash_pct < settings.CASH_BUFFER_CRITICAL_PCT:
            return [ConcentrationAlert(
                alert_type="CASH",
                severity="CRITICAL",
                message=f"Cash buffer at {cash_pct:.1f}% (critical minimum: {settings.CASH_BUFFER_CRITICAL_PCT:.0f}%)",
                current_value=cash_pct,
                threshold=settings.CASH_BUFFER_CRITICAL_PCT,
                affected=[],
                suggestion="Close positions before adding any new trades — cash critically low",
            )]
        if cash_pct < settings.CASH_BUFFER_WARNING_PCT:
            return [ConcentrationAlert(
                alert_type="CASH",
                severity="WARNING",
                message=f"Cash buffer at {cash_pct:.1f}% (minimum: {settings.CASH_BUFFER_WARNING_PCT:.0f}%)",
                current_value=cash_pct,
                threshold=settings.CASH_BUFFER_WARNING_PCT,
                affected=[],
                suggestion="Reduce position sizes or close some positions before adding new ones",
            )]
        return []

    # ── helpers ──────────────────────────────────────────────────────────────

    def _total_exposure(self, positions: list[Any], portfolio_value: float) -> float:
        total = 0.0
        for pos in positions:
            entry = abs(float(_get(pos, "entry_price", 0) or 0))
            qty = abs(int(_get(pos, "qty", 1) or 1))
            is_opt = _get(pos, "option_type") in ("put", "call")
            mult = settings.OPTIONS_CONTRACT_MULTIPLIER if is_opt else 1
            total += entry * qty * mult
        return total if total > 0 else portfolio_value

    def _portfolio_value(self) -> float:
        if self._paper is None:
            return 0.0
        try:
            return float(self._paper.get_state().equity or 0)
        except Exception as exc:
            logger.debug("ConcentrationChecker: equity lookup failed: %s", exc)
            return 0.0

    def _cash_pct(self, portfolio_value: float) -> float:
        if self._paper is not None:
            try:
                state = self._paper.get_state()
                equity = float(state.equity or 0)
                cash = float(state.cash or 0)
                if equity > 0:
                    return cash / equity * 100.0
            except Exception as exc:
                logger.debug("ConcentrationChecker: cash lookup failed: %s", exc)
        # Fall back to settings-derived cash buffer
        return float(settings.cash_buffer_pct)


__all__ = ["ConcentrationChecker", "ConcentrationAlert"]
