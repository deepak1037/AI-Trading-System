"""Dividend risk alerts for short put positions.

When an ex-dividend date falls before a short put's expiry, the stock drops
by roughly the dividend amount on that date. This check warns if the dividend
erodes more than DIVIDEND_RISK_THRESHOLD_PCT of the put's OTM buffer.

Data source: yfinance (Ticker.calendar for ex-div date, Ticker.dividends for amount).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class DividendAlert(BaseModel):
    ticker: str
    ex_div_date: date
    dividend_amount: float
    dividend_pct: float          # dividend as % of current stock price
    affected_put_strike: float
    put_itm_risk: bool           # True if dividend erodes > OTM buffer
    premium_collected: float
    risk_summary: str


def _get(pos: Any, key: str, default: Any = None) -> Any:
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


class DividendChecker:
    """Checks short put positions for ex-dividend date risk."""

    def check_dividend_risk(self, positions: list[Any]) -> list[DividendAlert]:
        """Return DividendAlert for each put that has ex-div before expiry."""
        alerts = []
        for pos in positions:
            opt_type = str(_get(pos, "option_type", "") or "").lower()
            if opt_type != "put":
                continue

            ticker = str(_get(pos, "ticker", "") or "")
            expiry_str = _get(pos, "expiry")
            strike = _get(pos, "strike")
            if not ticker or not expiry_str or strike is None:
                continue

            try:
                expiry = date.fromisoformat(str(expiry_str))
            except (ValueError, TypeError):
                continue

            alert = self._check_position(pos, ticker, strike, expiry)
            if alert:
                alerts.append(alert)

        return alerts

    def _check_position(
        self,
        pos: Any,
        ticker: str,
        strike: float,
        expiry: date,
    ) -> DividendAlert | None:
        ex_div = self._get_ex_dividend_date(ticker)
        if ex_div is None:
            return None

        # Only alert if ex-div falls before the expiry
        if ex_div > expiry or ex_div <= date.today():
            return None

        dividend = self._get_dividend_amount(ticker)
        if dividend <= 0:
            return None

        current_price = self._get_price(ticker, pos)
        if current_price <= 0:
            return None

        dividend_pct = dividend / current_price * 100.0
        otm_buffer = (current_price - strike) / current_price * 100.0
        if otm_buffer <= 0:
            otm_buffer = 0.01  # already ITM

        # Alert only when dividend consumes > threshold of the OTM buffer
        if dividend_pct <= otm_buffer * settings.DIVIDEND_RISK_THRESHOLD_PCT:
            return None

        premium = float(_get(pos, "entry_price", 0) or 0)
        put_itm = dividend_pct > otm_buffer

        return DividendAlert(
            ticker=ticker,
            ex_div_date=ex_div,
            dividend_amount=round(dividend, 4),
            dividend_pct=round(dividend_pct, 4),
            affected_put_strike=float(strike),
            put_itm_risk=put_itm,
            premium_collected=round(premium, 4),
            risk_summary=(
                f"{ticker} ex-div {ex_div}: "
                f"${dividend:.2f} ({dividend_pct:.2f}%) "
                f"vs {otm_buffer:.2f}% OTM buffer"
            ),
        )

    # ── data helpers (yfinance) ──────────────────────────────────────────────

    @staticmethod
    def _get_ex_dividend_date(ticker: str) -> date | None:
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                return None
            # calendar may be a dict or a DataFrame depending on yfinance version
            if isinstance(cal, dict):
                ex = cal.get("Ex-Dividend Date")
            else:
                try:
                    ex = cal.loc["Ex-Dividend Date"].iloc[0]
                except Exception:
                    return None
            if ex is None:
                return None
            if hasattr(ex, "date"):
                return ex.date()
            return date.fromisoformat(str(ex)[:10])
        except Exception as exc:
            logger.debug("DividendChecker: ex-div lookup failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _get_dividend_amount(ticker: str) -> float:
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            divs = yf.Ticker(ticker).dividends
            if divs is None or len(divs) == 0:
                return 0.0
            return float(divs.iloc[-1])
        except Exception as exc:
            logger.debug("DividendChecker: dividend lookup failed for %s: %s", ticker, exc)
            return 0.0

    @staticmethod
    def _get_price(ticker: str, pos: Any) -> float:
        # Prefer current_price already on the position
        cp = _get(pos, "underlying_price") or _get(pos, "current_price")
        if cp and float(cp) > 0:
            return float(cp)
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            info = yf.Ticker(ticker).fast_info
            return float(info.last_price or 0)
        except Exception as exc:
            logger.debug("DividendChecker: price lookup failed for %s: %s", ticker, exc)
            return 0.0


__all__ = ["DividendChecker", "DividendAlert"]
