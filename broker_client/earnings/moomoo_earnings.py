"""Earnings data connector (Phase 3, Step 1).

**Moomoo OpenD has no earnings-calendar endpoint** — it exposes quotes, option
chains, and option *volatility*, but not "who reports when". So the dates and the
IV come from different places and are combined:

    Dates  (when each company reports):
        manual CSV export → FMP earnings-calendar → yfinance calendar
    IV     (current implied vol, IV rank/percentile, expected move):
        Moomoo ``get_option_volatility`` (1-year series) per ticker

``EarningsDataProvider.get_upcoming_earnings`` resolves the dates, then — when
OpenD is reachable — enriches each event in place with real Moomoo IV via
``MoomooEarningsConnector.enrich_event_iv``. A manual CSV already carries the full
IV columns, so it is used as-is without re-querying Moomoo.

Every Moomoo call is best-effort and never raises out of the connector — a
gateway that is down simply yields dates-only events.
"""

from __future__ import annotations

import contextlib
import time
from datetime import date, datetime, timedelta, timezone
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

# get_option_volatility's queryTimePeriod uses Qot_Common.OptionVolatilityTimePeriodType
# (Year = 5). The SDK exposes NO friendly enum for it, and — critically —
# ft.RangePeriod.ONE_YEAR is a DIFFERENT enum whose value 3 means *Quarter* here.
_HV_PERIOD_365D = 4  # OptionHVPeriod.HV_365D (hvTimePeriod is a plain int field)


def _vol_year_period() -> int:
    """The protobuf value for a 1-year IV window (OptionVolatilityTimePeriodType_Year)."""
    try:
        from moomoo.common.pb.Qot_Common_pb2 import (  # type: ignore[import-untyped]
            OptionVolatilityTimePeriodType_Year,
        )

        return int(OptionVolatilityTimePeriodType_Year)
    except Exception:  # noqa: BLE001 — value is stable across SDK builds
        return 5


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
        """Always ``[]`` — Moomoo OpenD has no earnings-*calendar* endpoint.

        Dates come from FMP/yfinance/CSV (see ``EarningsDataProvider``); Moomoo's
        role is IV *enrichment* per ticker via ``enrich_event_iv``. Kept as a
        no-op so the provider's source chain stays uniform.
        """
        return []

    # ── IV enrichment (the real Moomoo value-add) ─────────────────────────────
    def enrich_event_iv(self, event: EarningsEvent) -> bool:
        """Fill ``event`` IV fields from Moomoo in place. True if IV was added.

        Primary source is the 1-year IV series (``get_option_volatility``), which
        yields current IV + IV rank/percentile. If that serves nothing, falls back
        to the ATM option's snapshot IV + straddle-implied expected move (no rank).
        Spot comes from the market snapshot. Never raises.
        """
        if not self._opend_reachable():
            return False
        try:
            spot = self.get_spot(event.ticker)
            if spot:
                event.stock_price = spot

            iv = self.get_underlying_iv(event.ticker)
            if iv is None:
                iv = self.get_atm_iv(event.ticker, spot, event.earnings_date)
            if iv is None:
                return False

            event.iv_current = iv["iv_current"]
            if iv.get("iv_rank") is not None:
                event.iv_rank = iv["iv_rank"]
            if iv.get("iv_percentile") is not None:
                event.iv_percentile = iv["iv_percentile"]
            event.expected_move = iv.get("expected_move") or self._expected_move(
                iv["iv_current"], event.earnings_date
            )
            event.source = "moomoo_iv"
            logger.info(
                "Moomoo IV %s: iv=%.1f%% rank=%s pct=%s exp_move=±%.1f%% (via %s)",
                event.ticker, event.iv_current, event.iv_rank, event.iv_percentile,
                event.expected_move, iv.get("iv_source", "?"),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Moomoo IV enrichment failed for %s: %s", event.ticker, exc)
            return False

    def _option_volatility_df(self, ticker: str) -> Any | None:
        """Raw 1-year IV/HV DataFrame from ``get_option_volatility``, or None.

        Surfaces the actual ``ret``/message on failure (the old code swallowed it,
        which is why a broken call looked like "no IV"). Uses the CORRECT period
        enum — ``OptionVolatilityTimePeriodType_Year`` (=5). The earlier code sent
        ``RangePeriod.ONE_YEAR`` (=3), which in this field's enum means *Quarter*.
        Falls back to the server-default window if the explicit period errors.
        """
        import moomoo as ft  # type: ignore[import-untyped]

        ctx = self._ensure_ctx()
        code = f"US.{ticker}"
        last: Any = "no response"
        for period in (_vol_year_period(), None):
            try:
                ret, df = ctx.get_option_volatility(
                    code, query_time_period=period, hv_time_period=_HV_PERIOD_365D
                )
            except Exception as exc:  # noqa: BLE001 — try the next variant
                last = repr(exc)
                continue
            if ret == ft.RET_OK and _has_rows(df) and "implied_volatility" in df:
                return df
            # On RET_ERROR moomoo returns (RET_ERROR, message_string).
            last = df if ret != ft.RET_OK else "empty IV series"
        logger.warning(
            "Moomoo get_option_volatility('%s') returned no IV (period tried "
            "Year+default): %s", code, last,
        )
        return None

    def get_underlying_iv(self, ticker: str) -> dict[str, Any] | None:
        """Current IV + IV rank/percentile + HV from Moomoo's 1-year IV series.

        ``get_option_volatility`` returns a time series of ``implied_volatility``
        / ``history_volatility`` for the underlying. IV rank is where current IV
        sits in its range; IV percentile is the fraction of days at or below it.
        Returns None when Moomoo serves no IV (logged) — the caller then tries the
        ATM-chain fallback.
        """
        df = self._option_volatility_df(ticker)
        if df is None:
            return None

        frame = df.sort_values("timestamp") if "timestamp" in df else df
        series = [
            iv for iv in (_normalize_iv(v) for v in frame["implied_volatility"])
            if iv is not None
        ]
        if not series:
            return None
        current = series[-1]
        lo, hi = min(series), max(series)
        iv_rank = 100.0 * (current - lo) / (hi - lo) if hi > lo else 50.0
        iv_percentile = 100.0 * sum(1 for v in series if v <= current) / len(series)
        hv = None
        if "history_volatility" in frame:
            hv = _normalize_iv(frame["history_volatility"].iloc[-1])
        return {
            "iv_current": round(current, 2),
            "iv_rank": int(round(max(0.0, min(iv_rank, 100.0)))),
            "iv_percentile": int(round(max(0.0, min(iv_percentile, 100.0)))),
            "hv_current": hv,
            "samples": len(series),
            "iv_source": "option_volatility",
        }

    def get_atm_iv(
        self, ticker: str, spot: float | None, earnings_date: date
    ) -> dict[str, Any] | None:
        """Fallback IV from the ATM option's snapshot (no IV rank).

        Used when ``get_option_volatility`` serves nothing (e.g. no option-vol
        data entitlement). Picks the nearest expiry on/after earnings, finds the
        ATM call+put, and reads ``option_implied_volatility`` + ``option_premium``
        from ``get_market_snapshot`` — the same snapshot call that already works.
        Expected move comes from the ATM straddle. IV rank/percentile are left
        absent (a single point can't be ranked). Returns None on any gap.
        """
        import moomoo as ft  # type: ignore[import-untyped]

        ctx = self._ensure_ctx()
        code = f"US.{ticker}"
        try:
            if spot is None:
                spot = self.get_spot(ticker)
            if not spot or spot <= 0:
                return None
            expiry = self._nearest_expiry(ctx, code, earnings_date)
            if expiry is None:
                logger.warning("Moomoo ATM IV %s: no option expiry found", ticker)
                return None
            ret, chain = ctx.get_option_chain(code, start=expiry, end=expiry)
            if ret != ft.RET_OK or not _has_rows(chain):
                logger.warning("Moomoo ATM IV %s: option chain empty (%s)", ticker, chain)
                return None
            call_code, put_code = self._atm_codes(chain, spot)
            if not call_code or not put_code:
                return None
            ret, snap = ctx.get_market_snapshot([call_code, put_code])
            if ret != ft.RET_OK or not _has_rows(snap):
                logger.warning("Moomoo ATM IV %s: option snapshot empty (%s)", ticker, snap)
                return None
            return self._atm_from_snapshot(snap, spot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Moomoo ATM IV failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _nearest_expiry(ctx: Any, code: str, earnings_date: date) -> str | None:
        """First listed option expiry on/after earnings (else the latest)."""
        import moomoo as ft  # type: ignore[import-untyped]

        ret, exp = ctx.get_option_expiration_date(code)
        if ret != ft.RET_OK or not _has_rows(exp) or "strike_time" not in exp:
            return None
        dates = sorted(str(s) for s in exp["strike_time"] if s)
        for s in dates:
            d = _parse_any_date(s)
            if d is not None and d >= earnings_date:
                return s
        return dates[-1] if dates else None

    @staticmethod
    def _atm_codes(chain: Any, spot: float) -> tuple[str | None, str | None]:
        """ATM call + put option codes (strike closest to spot)."""
        rows = chain.to_dict("records") if hasattr(chain, "to_dict") else list(chain)
        calls = [r for r in rows if str(r.get("option_type", "")).upper().endswith("CALL")]
        puts = [r for r in rows if str(r.get("option_type", "")).upper().endswith("PUT")]

        def _closest(rs: list[dict]) -> str | None:
            usable = [r for r in rs if _num(r.get("strike_price")) is not None]
            if not usable:
                return None
            best = min(usable, key=lambda r: abs(_num(r["strike_price"]) - spot))  # type: ignore[operator]
            return str(best.get("code")) if best.get("code") else None

        return _closest(calls), _closest(puts)

    @staticmethod
    def _atm_from_snapshot(snap: Any, spot: float) -> dict[str, Any] | None:
        """ATM IV (avg of call/put) + straddle-implied expected move."""
        rows = snap.to_dict("records") if hasattr(snap, "to_dict") else list(snap)
        ivs: list[float] = []
        prems: list[float] = []
        for r in rows:
            iv = _normalize_iv(r.get("option_implied_volatility"))
            if iv is not None:
                ivs.append(iv)
            prem = _num(r.get("option_premium"))
            if prem is None:
                prem = _num(r.get("last_price"))
            if prem is not None:
                prems.append(prem)
        if not ivs:
            return None
        iv_current = round(sum(ivs) / len(ivs), 2)
        expected_move = (
            round(sum(prems) / spot * 100.0, 2) if prems and spot > 0 else 0.0
        )
        return {
            "iv_current": iv_current,
            "iv_rank": None,            # single point — not rankable
            "iv_percentile": None,
            "hv_current": None,
            "expected_move": expected_move,
            "iv_source": "atm_chain",
        }

    def get_spot(self, ticker: str) -> float | None:
        """Underlying last price from a Moomoo market snapshot (None on gap)."""
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_ctx()
            ret, data = ctx.get_market_snapshot([f"US.{ticker}"])
            if ret != ft.RET_OK or not _has_rows(data):
                return None
            row = data.iloc[0].to_dict() if hasattr(data, "iloc") else dict(data)
            return _num(row.get("last_price"))
        except Exception as exc:
            logger.debug("Moomoo spot fetch failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _expected_move(iv_pct: float, earnings_date: date) -> float:
        """±% expected move to the post-earnings expiry, from IV and DTE.

        A 1-sigma move over ``T`` years ≈ IV·√T. We use the calendar days to the
        first weekly expiry that captures the print (earnings + ~2 days).
        """
        import math

        dte = max((earnings_date - date.today()).days, 0) + 2
        return round(iv_pct * math.sqrt(dte / 365.0), 2)

    def get_earnings_iv_history(self, ticker: str) -> EarningsIVHistory:
        """Per-quarter IV-crush history, *computed* from Moomoo's IV series.

        Moomoo OpenD has no "Historical Earnings Data" table, but it does serve a
        daily implied-vol series (``get_option_volatility``). We pair that with
        past earnings dates (FMP) and the realized close-to-close move (yfinance)
        to reconstruct, per quarter:

          * ``iv_before`` / ``iv_after`` / ``iv_crush`` — IV the trading day before
            the print vs a few days after, and the % collapse between them.
          * ``expected_move`` — the IV-implied 1-sigma move over that same window.
          * ``actual_move_close`` — what the stock actually did (for breach rate).

        Best-effort: returns an empty history (→ analyzer's summary path) if OpenD
        is down or any source is missing. Never raises.
        """
        if not self._opend_reachable():
            return EarningsIVHistory(ticker=ticker, quarters=[])
        try:
            series = self._iv_series(ticker)
            past = _past_earnings_dates(ticker)
            if not series or not past:
                return EarningsIVHistory(ticker=ticker, quarters=[])
            moves = _earnings_moves(ticker, past)
            quarters: list[QuarterlyData] = []
            for ed in past:
                q = self._quarter_from_series(ed, series, moves.get(ed))
                if q is not None:
                    quarters.append(q)
            logger.debug("Computed %d IV-crush quarters for %s", len(quarters), ticker)
            return EarningsIVHistory(ticker=ticker, quarters=quarters)
        except Exception as exc:
            logger.debug("Moomoo IV history failed for %s: %s", ticker, exc)
            return EarningsIVHistory(ticker=ticker, quarters=[])

    def _iv_series(self, ticker: str) -> list[tuple[date, float]]:
        """Daily (date, IV%) series for the underlying, oldest-first."""
        df = self._option_volatility_df(ticker)
        if df is None:
            return []
        out: list[tuple[date, float]] = []
        for _, row in df.iterrows():
            d = _parse_any_date(row.get("timestamp_str")) or _epoch_to_date(row.get("timestamp"))
            iv = _normalize_iv(row.get("implied_volatility"))
            if d is not None and iv is not None:
                out.append((d, iv))
        out.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _quarter_from_series(
        earnings_date: date,
        series: list[tuple[date, float]],
        actual_move: float | None,
    ) -> QuarterlyData | None:
        """One quarter's crush from IV just-before vs just-after the print."""
        import math

        before = [iv for d, iv in series if 0 <= (earnings_date - d).days <= 3]
        after = [iv for d, iv in series if 1 <= (d - earnings_date).days <= 4]
        if not before or not after:
            return None
        iv_before = before[-1]        # closest trading day at/just-before earnings
        iv_after = after[0]           # first sample after the print
        if iv_before <= 0:
            return None
        crush = (iv_before - iv_after) / iv_before * 100.0
        window_days = 3               # ~before→after holding window
        expected_move = iv_before * math.sqrt(window_days / 365.0)
        return QuarterlyData(
            quarter=earnings_date.isoformat(),
            earnings_date=earnings_date,
            iv_before=round(iv_before, 2),
            iv_after=round(iv_after, 2),
            iv_crush=round(crush, 2),
            expected_move=round(expected_move, 2),
            actual_move_close=actual_move,
        )

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
        """Resolve earnings dates, then enrich each with Moomoo IV in place.

        Dates: manual CSV → FMP calendar → yfinance. IV: Moomoo
        ``get_option_volatility`` per ticker (when OpenD is reachable). A manual
        CSV already carries full IV, so it is not re-queried.
        """
        days = days_ahead if days_ahead is not None else settings.EARNINGS_DAYS_AHEAD
        events, source, has_iv = self._resolve_dates(days)
        if not events:
            logger.warning("No earnings data from any source (CSV/FMP/yfinance)")
            return []

        events = self._within_window(events, days)
        enriched = 0 if has_iv else self._enrich_iv(events)

        if enriched:
            logger.info(
                "Earnings source: %s dates + Moomoo IV (%d/%d enriched)",
                source, enriched, len(events),
            )
        elif has_iv:
            logger.info("Earnings source: %s (%d events, IV included)", source, len(events))
        else:
            logger.info(
                "Earnings source: %s (%d events, dates only — Moomoo IV unavailable)",
                source, len(events),
            )
        return events

    def _resolve_dates(self, days: int) -> tuple[list[EarningsEvent], str, bool]:
        """(events, source_label, dates_already_have_iv). First non-empty wins."""
        # A legacy/native Moomoo events hook (normally empty — no calendar API).
        events = self.connector.get_upcoming_earnings(days)
        if events:
            return events, "Moomoo OpenD", True

        events = self.manual.load()
        if events:
            return events, "manual CSV", True  # CSV carries full IV columns

        events = self._fmp_calendar(days)
        if events:
            return events, "FMP calendar", False

        events = self._yfinance_dates(days)
        if events:
            return events, "yfinance", False

        return [], "none", False

    def _enrich_iv(self, events: list[EarningsEvent]) -> int:
        """Enrich dates-only events with Moomoo IV. Returns how many got IV."""
        if not self.connector._opend_reachable():
            return 0
        enriched = 0
        for event in events:
            if event.iv_current and event.iv_current > 0:
                continue  # already has IV (e.g. from CSV)
            if self.connector.enrich_event_iv(event):
                enriched += 1
            time.sleep(settings.MOOMOO_PACE_SECONDS)  # 30 req/30s OpenD cap
        return enriched

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


def _normalize_iv(value: Any) -> float | None:
    """Implied/historical vol as a percent.

    Moomoo returns IV as a percent (45.2 = 45.2%) on most fields, but some feeds
    give a fraction (0.452). Values < 3 are treated as fractions and scaled up;
    non-positive / sentinel values are dropped.
    """
    v = _num(value)
    if v is None or v <= 0:
        return None
    return round(v, 2) if v >= 3 else round(v * 100.0, 2)


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


def _epoch_to_date(value: Any) -> date | None:
    """Epoch seconds (Moomoo IV-series timestamp) → date. None on any gap."""
    v = _num(value)
    if v is None or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def _past_earnings_dates(ticker: str, lookback_days: int = 400, limit: int = 8) -> list[date]:
    """Past earnings dates (most-recent first) from FMP, within the IV window.

    Used to align Moomoo's IV series to each prior print. Returns [] when FMP is
    unconfigured or the call fails — the caller then yields no IV history.
    """
    if not settings.FMP_API_KEY:
        return []
    today = date.today()
    earliest = today - timedelta(days=lookback_days)
    try:
        resp = requests.get(
            f"{_FMP_BASE}/earnings",
            params={"symbol": ticker.upper(), "apikey": settings.FMP_API_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        dates: list[date] = []
        for row in resp.json() or []:
            ed = _parse_any_date(row.get("date"))
            if ed is not None and earliest <= ed < today:
                dates.append(ed)
        return sorted(set(dates), reverse=True)[:limit]
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("FMP past earnings failed for %s: %s", ticker, exc)
        return []


def _earnings_moves(ticker: str, dates: list[date]) -> dict[date, float]:
    """Signed close-to-close % move around each earnings date (best-effort).

    Uses yfinance daily closes: the move from the last close before the print to
    the first close after it. Missing data degrades to an absent entry (the
    quarter then carries no actual-move, so it's skipped from breach analysis).
    """
    if not dates:
        return {}
    try:
        import pandas as pd  # noqa: F401
        import yfinance as yf

        start = (min(dates) - timedelta(days=5)).isoformat()
        end = (max(dates) + timedelta(days=6)).isoformat()
        hist = yf.Ticker(ticker).history(start=start, end=end)
        if hist is None or hist.empty:
            return {}
        closes = hist["Close"]
        idx_dates = [d.date() for d in closes.index]
        out: dict[date, float] = {}
        for ed in dates:
            before = [(d, c) for d, c in zip(idx_dates, closes) if d <= ed]
            after = [(d, c) for d, c in zip(idx_dates, closes) if d > ed]
            if not before or not after:
                continue
            prev_close = before[-1][1]
            post_close = after[0][1]
            if prev_close:
                out[ed] = round((post_close - prev_close) / prev_close * 100.0, 2)
        return out
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("Earnings moves failed for %s: %s", ticker, exc)
        return {}


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


__all__ = ["MoomooEarningsConnector", "EarningsDataProvider"]
