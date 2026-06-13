"""Earnings data connector (Phase 3, Step 1).

``MoomooEarningsConnector`` talks to the local Moomoo OpenD gateway for upcoming
earnings + per-quarter IV history. Because OpenD's earnings-IV endpoints require
permissions that may not be granted, the ``EarningsDataProvider`` wraps it in a
priority chain that always returns *something* usable:

    Priority 1: Moomoo OpenD API (full IV + per-quarter history)
    Priority 2: Manual CSV export (broker_client/earnings/data/)
    Priority 3: FMP earnings calendar (dates only, no IV)
    Priority 4: yfinance earnings dates (dates only, no IV)

Every Moomoo call is best-effort and never raises out of the connector — a
gateway that is down simply degrades to the next source.
"""

from __future__ import annotations

import contextlib
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests

from broker_client.earnings.manual_input import ManualEarningsInput
from broker_client.earnings.models import (
    EarningsEvent,
    EarningsIVHistory,
    QuarterlyData,
)
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_FMP_BASE = "https://financialmodelingprep.com/stable"


class MoomooEarningsConnector:
    """Best-effort Moomoo OpenD earnings/IV connector.

    Lazily connects on first use (never at construction) so importing or
    instantiating it in tests/CI without a running OpenD gateway is safe.
    """

    def __init__(self) -> None:
        self._ctx: Any = None

    # ── connection lifecycle ──────────────────────────────────────────────────
    @staticmethod
    def _opend_reachable(timeout: float = 1.0) -> bool:
        """Fast TCP probe of the OpenD gateway.

        The moomoo SDK's ``OpenQuoteContext`` retries a refused connection
        forever in a background thread, so constructing it when OpenD is down
        hangs the caller. Probe the socket first and bail cleanly instead.
        """
        import socket

        try:
            with socket.create_connection(
                (settings.MOOMOO_HOST, settings.MOOMOO_PORT), timeout=timeout
            ):
                return True
        except OSError:
            return False

    def _ensure_ctx(self) -> Any:
        if self._ctx is None:
            import moomoo as ft  # type: ignore[import-untyped]

            self._ctx = ft.OpenQuoteContext(
                host=settings.MOOMOO_HOST, port=settings.MOOMOO_PORT
            )
        return self._ctx

    def close(self) -> None:
        if self._ctx is not None:
            with contextlib.suppress(Exception):
                self._ctx.close()
            self._ctx = None

    def available(self) -> bool:
        """True if the OpenD gateway is reachable for a snapshot call."""
        if not self._opend_reachable():
            return False
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_ctx()
            ret, _ = ctx.get_market_snapshot(["US.AAPL"])
            return bool(ret == ft.RET_OK)
        except Exception as exc:
            logger.debug("Moomoo OpenD not available: %s", exc)
            return False

    # ── upcoming earnings ─────────────────────────────────────────────────────
    def get_upcoming_earnings(self, days_ahead: int = 14) -> list[EarningsEvent]:
        """Fetch upcoming earnings + IV from Moomoo (``[]`` when unavailable).

        OpenD exposes earnings calendar + IV via per-ticker snapshots. We probe
        the watchlist tickers and keep those whose next report falls inside the
        window. Pacing respects the 30 req/30s OpenD cap.
        """
        try:
            if not self._opend_reachable():
                logger.debug("Moomoo OpenD not reachable — skipping to next source")
                return []
            tickers = self._watchlist_tickers()
            if not tickers:
                return []
            start = date.today()
            end = start + timedelta(days=days_ahead)
            events: list[EarningsEvent] = []
            for ticker in tickers:
                event = self._fetch_earnings_iv(ticker)
                if event is not None and start <= event.earnings_date <= end:
                    events.append(event)
                time.sleep(settings.MOOMOO_PACE_SECONDS)
            return events
        except Exception as exc:
            logger.debug("Moomoo upcoming earnings failed: %s", exc)
            return []

    def _fetch_earnings_iv(self, ticker: str) -> EarningsEvent | None:
        """One ticker → EarningsEvent from a Moomoo snapshot. None on any gap."""
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_ctx()
            ret, data = ctx.get_market_snapshot([f"US.{ticker}"])
            if ret != ft.RET_OK or not _has_rows(data):
                return None
            row = data.iloc[0].to_dict() if hasattr(data, "iloc") else dict(data)
            ed = _parse_any_date(row.get("earnings_date") or row.get("report_date"))
            if ed is None:
                return None
            return EarningsEvent(
                ticker=ticker,
                earnings_date=ed,
                iv_current=_num(row.get("option_implied_volatility")) or 0.0,
                stock_price=_num(row.get("last_price")),
                source="moomoo",
            )
        except Exception as exc:
            logger.debug("Moomoo IV fetch failed for %s: %s", ticker, exc)
            return None

    def get_earnings_iv_history(self, ticker: str) -> EarningsIVHistory:
        """Per-quarter IV crush / expected-move history (empty when unavailable).

        Mirrors Moomoo's "Historical Earnings Data" table. Returns an empty
        history rather than raising so the analyzer can fall back to the summary
        fields on the ``EarningsEvent`` instead.
        """
        if not self._opend_reachable():
            return EarningsIVHistory(ticker=ticker, quarters=[])
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_ctx()
            # OpenD has no dedicated earnings-IV-history endpoint on the free
            # tier; this hook exists for installs that do. Probe and degrade.
            getter = getattr(ctx, "get_history_kl_quote", None)
            if getter is None:
                return EarningsIVHistory(ticker=ticker, quarters=[])
            ret, data = getter(f"US.{ticker}")  # pragma: no cover - perms-gated
            if ret != ft.RET_OK or not _has_rows(data):  # pragma: no cover
                return EarningsIVHistory(ticker=ticker, quarters=[])
            quarters = [self._row_to_quarter(r) for r in _rows(data)]  # pragma: no cover
            return EarningsIVHistory(  # pragma: no cover
                ticker=ticker, quarters=[q for q in quarters if q is not None]
            )
        except Exception as exc:
            logger.debug("Moomoo IV history failed for %s: %s", ticker, exc)
            return EarningsIVHistory(ticker=ticker, quarters=[])

    @staticmethod
    def _row_to_quarter(row: dict[str, Any]) -> QuarterlyData | None:  # pragma: no cover
        try:
            return QuarterlyData(
                quarter=str(row.get("period_text", "")),
                iv_crush=_num(row.get("iv_crush")),
                expected_move=_num(row.get("expected_move")),
                actual_move_close=_num(row.get("actual_move")),
            )
        except Exception:
            return None

    @staticmethod
    def _watchlist_tickers() -> list[str]:
        """Active watchlist tickers (quality filter), or [] if unavailable."""
        try:
            from data.db import get_connection

            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT ticker FROM watchlist WHERE is_active=1"
                ).fetchall()
            return [r["ticker"] for r in rows]
        except Exception as exc:
            logger.debug("Watchlist fetch failed: %s", exc)
            return []


class EarningsDataProvider:
    """Resolves upcoming earnings through the Step-1 priority chain."""

    def __init__(
        self,
        connector: MoomooEarningsConnector | None = None,
        manual: ManualEarningsInput | None = None,
    ) -> None:
        self.connector = connector or MoomooEarningsConnector()
        self.manual = manual or ManualEarningsInput()

    def get_upcoming_earnings(self, days_ahead: int | None = None) -> list[EarningsEvent]:
        """First source that returns events wins; logs which one was used."""
        days = days_ahead if days_ahead is not None else settings.EARNINGS_DAYS_AHEAD

        events = self.connector.get_upcoming_earnings(days)
        if events:
            logger.info("Earnings source: Moomoo OpenD (%d events)", len(events))
            return self._within_window(events, days)

        events = self.manual.load()
        if events:
            logger.info("Earnings source: manual CSV (%d events)", len(events))
            return self._within_window(events, days)

        events = self._fmp_calendar(days)
        if events:
            logger.info("Earnings source: FMP calendar (%d events, dates only)", len(events))
            return events

        events = self._yfinance_dates(days)
        if events:
            logger.info("Earnings source: yfinance (%d events, dates only)", len(events))
            return events

        logger.warning("No earnings data from any source (Moomoo/CSV/FMP/yfinance)")
        return []

    def get_iv_history(self, ticker: str) -> EarningsIVHistory:
        return self.connector.get_earnings_iv_history(ticker)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _within_window(events: list[EarningsEvent], days: int) -> list[EarningsEvent]:
        start = date.today()
        end = start + timedelta(days=days)
        return [e for e in events if start <= e.earnings_date <= end]

    def _fmp_calendar(self, days: int) -> list[EarningsEvent]:
        if not settings.FMP_API_KEY:
            return []
        start = date.today()
        end = start + timedelta(days=days)
        try:
            resp = requests.get(
                f"{_FMP_BASE}/earnings-calendar",
                params={
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "apikey": settings.FMP_API_KEY,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            out: list[EarningsEvent] = []
            for row in resp.json() or []:
                ed = _parse_any_date(row.get("date"))
                sym = str(row.get("symbol", "")).upper()
                if ed is None or not sym:
                    continue
                out.append(
                    EarningsEvent(
                        ticker=sym,
                        earnings_date=ed,
                        earnings_time=_fmp_time(row.get("time")),
                        source="fmp",
                    )
                )
            return out
        except Exception as exc:
            logger.debug("FMP earnings calendar failed: %s", exc)
            return []

    def _yfinance_dates(self, days: int) -> list[EarningsEvent]:
        """Dates-only fallback from yfinance for active watchlist tickers."""
        tickers = MoomooEarningsConnector._watchlist_tickers()
        if not tickers:
            return []
        start = date.today()
        end = start + timedelta(days=days)
        out: list[EarningsEvent] = []
        try:
            import yfinance as yf
        except Exception:
            return []
        for ticker in tickers:
            try:
                cal = yf.Ticker(ticker).calendar
                ed = None
                if isinstance(cal, dict):
                    vals = cal.get("Earnings Date")
                    ed = vals[0] if isinstance(vals, list) and vals else vals
                ed_date = _parse_any_date(ed)
                if ed_date is not None and start <= ed_date <= end:
                    out.append(
                        EarningsEvent(ticker=ticker, earnings_date=ed_date, source="yfinance")
                    )
            except Exception as exc:
                logger.debug("yfinance earnings date failed for %s: %s", ticker, exc)
        return out


# ── module-level parsing helpers ───────────────────────────────────────────────
def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # Reject obvious sentinels.
    return None if f != f or abs(f) >= 1e12 else f  # f != f catches NaN


def _parse_any_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    date_attr = getattr(value, "date", None)
    if callable(date_attr):
        try:
            result = date_attr()
            if isinstance(result, date):
                return result
        except Exception:
            pass
    s = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _fmp_time(raw: object) -> str:
    t = str(raw or "").strip().lower()
    if t in ("bmo", "before market open"):
        return "BMO"
    if t in ("amc", "after market close"):
        return "AMC"
    return "AMC"


def _has_rows(data: Any) -> bool:
    if data is None:
        return False
    if hasattr(data, "empty"):
        return not data.empty
    if isinstance(data, list | tuple):
        return len(data) > 0
    return bool(data)


def _rows(data: Any) -> list[dict[str, Any]]:  # pragma: no cover - perms-gated path
    if hasattr(data, "to_dict"):
        return list(data.to_dict("records"))
    if isinstance(data, list):
        return [r if isinstance(r, dict) else dict(r) for r in data]
    return []


__all__ = ["MoomooEarningsConnector", "EarningsDataProvider"]
