"""Scanner Stage 3: Fundamental inflection screen (Days 13-18).

Filters the liquidity-passing universe to fundamentally healthy names by:
  - EPS growth (quarterly / YoY)
  - Revenue growth (quarterly / YoY)
  - Analyst estimate revisions (FMP only)

Data source priority:
  1. Financial Modeling Prep (FMP) free API when ``FMP_API_KEY`` is set —
     reliable quarterly income statements + analyst estimates.
  2. Fallback: yfinance ``.info`` dict (trailingEps, revenueGrowth,
     earningsGrowth, grossMargins) with lenient thresholds, when no FMP key.

yfinance's earnings *calendar* (``earnings_dates`` / ``quarterly_earnings``) is
NOT used — it now returns "No earnings dates found" for nearly every ticker.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# FMP migrated off the legacy /api/v3 endpoints (Aug 2025) to /stable.
_FMP_BASE = "https://financialmodelingprep.com/stable"


class FundamentalScreen:
    """Stage 3: EPS growth + revenue growth + estimate revisions."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._fmp_key = settings.FMP_API_KEY
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "AI-Trading-System/1.0"
        # Resolved lazily on first screen() so construction stays network-free.
        self._source: Optional[str] = None  # "moomoo" | "fmp" | "yfinance"
        self._moomoo_ctx: Any = None

    def _resolve_source(self) -> str:
        """Pick the fundamentals source once, log it, and cache the decision.

        Priority:
          1. Moomoo OpenD (free, local, accurate non-GAAP quarterly EPS)
          2. FMP — only if a key can actually pull fundamentals (paid plan; the
             free tier returns 402 for income statements)
          3. yfinance ``.info`` fallback (GAAP earnings, always available)
        """
        if self._source is not None:
            return self._source
        if self._moomoo_available():
            self._source = "moomoo"
            logger.info("Scanner data source: Moomoo")
        elif bool(self._fmp_key) and self._fmp_fundamentals_available():
            self._source = "fmp"
            logger.info("Scanner data source: FMP")
        else:
            self._source = "yfinance"
            reason = (
                "FMP key set but fundamentals not accessible (free tier 402)"
                if self._fmp_key else "no Moomoo OpenD, no FMP key"
            )
            logger.warning("Scanner data source: yfinance fallback — %s.", reason)
        return self._source

    # ── Moomoo OpenD path (priority 1) ───────────────────────────────────────
    def _ensure_moomoo_ctx(self) -> Any:
        if self._moomoo_ctx is None:
            import moomoo as ft  # type: ignore[import-untyped]

            self._moomoo_ctx = ft.OpenQuoteContext(
                host=settings.MOOMOO_HOST, port=settings.MOOMOO_PORT
            )
        return self._moomoo_ctx

    def _close_moomoo(self) -> None:
        if self._moomoo_ctx is not None:
            try:
                self._moomoo_ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self._moomoo_ctx = None

    def _moomoo_available(self) -> bool:
        """True if OpenD is reachable and serves quarterly financials."""
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_moomoo_ctx()
            ret, data = ctx.get_financials_statements(
                "US.CRWD", statement_type=1, financial_type=10, num=2
            )
            return ret == ft.RET_OK and isinstance(data, dict) and bool(data.get("report_list"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Moomoo OpenD not available: %s", exc)
            return False

    def _moomoo_flags(self, ticker: str) -> Optional[dict]:
        """Fundamental flags from Moomoo quarterly statements, or None if failing.

        Income statement field ids: 8048 = Diluted EPS, 8001 = Total Revenue.
        ``report_list`` interleaves quarterly and annual (FY) rows — we keep only
        quarters and compare most-recent-first.
        """
        try:
            import moomoo as ft  # type: ignore[import-untyped]

            ctx = self._ensure_moomoo_ctx()
            ret, data = ctx.get_financials_statements(
                f"US.{ticker}", statement_type=1, financial_type=10, num=12
            )
            if ret != ft.RET_OK or not isinstance(data, dict):
                return None
            quarters = []
            for rep in data.get("report_list", []):
                if "Q" not in str(rep.get("period_text", "")).upper():
                    continue  # skip annual (FY) rows
                items = {it["field_id"]: it for it in rep.get("item_list", [])}
                quarters.append((
                    self._num(items.get(8048, {}).get("data")),  # diluted EPS
                    self._num(items.get(8001, {}).get("data")),  # revenue
                ))
            if len(quarters) < 2:
                return None
            eps = [q[0] for q in quarters]
            rev = [q[1] for q in quarters]
            eps_qoq = eps[0] is not None and eps[1] is not None and eps[0] > eps[1]
            eps_yoy = (
                len(eps) >= 5 and eps[0] is not None and eps[4] is not None
                and eps[0] > eps[4]
            )
            rev_yoy = (
                len(rev) >= 5 and rev[0] is not None and rev[4] is not None
                and rev[4] > 0 and rev[0] > rev[4] * 1.05
            )
            flags = {
                "eps_accelerating": bool(eps_qoq or eps_yoy),
                "rev_reaccelerating": bool(rev_yoy),
                "est_revisions_up": bool(eps_qoq and eps_yoy),
            }
            logger.debug("Stage3 Moomoo %s: %s", ticker, flags)
            return flags if any(flags.values()) else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Moomoo flags failed for %s: %s", ticker, exc)
            return None

    # ── FMP path ─────────────────────────────────────────────────────────────
    def _fmp_get(self, endpoint: str, **params) -> Any:
        params["apikey"] = self._fmp_key
        try:
            resp = self._session.get(
                f"{_FMP_BASE}/{endpoint}", params=params, timeout=15
            )
            if resp.status_code == 429:
                logger.warning("FMP rate limited on %s — backing off", endpoint)
                time.sleep(2.0)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("FMP %s failed: %s", endpoint, exc)
            return None

    def _fmp_fundamentals_available(self) -> bool:
        """Probe whether the key can pull income statements for the whole universe.

        The FMP free tier allows only a small demo set of megacaps (AAPL, MSFT,
        …) and returns 402 for everything else. Probe with a non-megacap symbol
        (CRWD) so a free key is correctly detected as unusable for the scan.
        """
        data = self._fmp_get("income-statement", symbol="CRWD", period="quarter", limit=1)
        return isinstance(data, list) and bool(data) and "revenue" in data[0]

    def _fmp_flags(self, ticker: str) -> Optional[dict]:
        """Return fundamental flags from FMP quarterly data, or None if failing.

        Flags: ``eps_accelerating``, ``rev_reaccelerating``, ``est_revisions_up``.
        A ticker passes if any flag is True.
        """
        income = self._fmp_get(
            "income-statement", symbol=ticker, period="quarter", limit=8
        )
        if not isinstance(income, list) or len(income) < 2:
            return None

        eps = [self._num(q.get("epsDiluted", q.get("eps"))) for q in income]
        rev = [self._num(q.get("revenue")) for q in income]

        eps_qoq = eps[0] is not None and eps[1] is not None and eps[0] > eps[1]
        eps_yoy = (
            len(eps) >= 5 and eps[0] is not None and eps[4] is not None
            and eps[4] != 0 and eps[0] > eps[4]
        )
        rev_yoy = (
            len(rev) >= 5 and rev[0] is not None and rev[4] is not None
            and rev[4] > 0 and rev[0] > rev[4] * 1.05
        )
        flags = {
            "eps_accelerating": bool(eps_qoq or eps_yoy),
            "rev_reaccelerating": bool(rev_yoy),
            "est_revisions_up": bool(self._fmp_estimates_positive(ticker)),
        }
        logger.debug("Stage3 FMP %s: %s", ticker, flags)
        return flags if any(flags.values()) else None

    def _fmp_estimates_positive(self, ticker: str) -> bool:
        """Best-effort: analysts estimating rising EPS next period vs current."""
        data = self._fmp_get(
            "analyst-estimates", symbol=ticker, period="quarter", limit=2
        )
        if not isinstance(data, list) or len(data) < 2:
            return False
        # FMP returns most-recent first; field name varies by API version.
        def _eps_avg(row: dict) -> Optional[float]:
            return self._num(row.get("epsAvg", row.get("estimatedEpsAvg")))
        nxt, cur = _eps_avg(data[0]), _eps_avg(data[1])
        return nxt is not None and cur is not None and cur != 0 and nxt > cur

    # ── Fallback path (yfinance .info, no earnings calendar) ─────────────────
    def _fallback_flags(self, ticker: str) -> Optional[dict]:
        """Return fundamental flags from yfinance ``.info``, or None if failing.

        No earnings calendar (it is unreliable). Maps available ``.info`` fields
        to the same three flags the scorer expects. Lenient — a name passes if
        any flag is True.
        """
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Stage3 fallback: .info failed for %s: %s", ticker, exc)
            return None
        if not info:
            return None

        trailing_eps = self._num(info.get("trailingEps"))
        rev_growth = self._num(info.get("revenueGrowth"))
        earn_growth = self._num(info.get("earningsGrowth")) or self._num(
            info.get("earningsQuarterlyGrowth")
        )
        gross_margin = self._num(info.get("grossMargins"))

        profitable = trailing_eps is not None and trailing_eps > 0
        healthy_margin = gross_margin is not None and gross_margin > 0.25

        flags = {
            # earnings growth → EPS acceleration proxy
            "eps_accelerating": bool(earn_growth is not None and earn_growth > 0),
            # revenue growth → revenue re-acceleration proxy
            "rev_reaccelerating": bool(rev_growth is not None and rev_growth > 0),
            # profitable + healthy margin → positive-outlook proxy (no estimates in .info)
            "est_revisions_up": bool(profitable and healthy_margin),
        }
        # Strong top-line growth alone is enough to keep a name in.
        if rev_growth is not None and rev_growth > 0.10:
            flags["rev_reaccelerating"] = True
        logger.debug(
            "Stage3 fallback %s: eps=%s revG=%s earnG=%s gm=%s -> %s",
            ticker, trailing_eps, rev_growth, earn_growth, gross_margin, flags,
        )
        return flags if any(flags.values()) else None

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _num(value: object) -> Optional[float]:
        """Coerce to float, returning None for missing/invalid values."""
        if value is None:
            return None
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    # ── Public API ───────────────────────────────────────────────────────────
    def screen(self, tickers: list[str]) -> list[dict]:
        """Run Stage 3 screening. Returns per-stock dicts for passing tickers::

            {ticker, eps_accelerating, rev_reaccelerating, est_revisions_up}

        so downstream scoring uses real fundamental flags, not a flat value.
        """
        source = self._resolve_source()
        # Only the yfinance .info path is rate-limited; Moomoo is local, FMP paid.
        pace = settings.SCANNER_YF_PACE_SECONDS if source == "yfinance" else 0.0
        flag_fn = {
            "moomoo": self._moomoo_flags,
            "fmp": self._fmp_flags,
            "yfinance": self._fallback_flags,
        }[source]

        passing: list[dict] = []
        try:
            for ticker in tickers:
                if pace:
                    time.sleep(pace)
                try:
                    flags = flag_fn(ticker)
                    if flags is not None:
                        passing.append({"ticker": ticker, **flags})
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Stage3: error for %s: %s", ticker, exc)
        finally:
            self._close_moomoo()

        logger.info(
            "Stage 3 fundamental screen (%s): %d/%d pass", source, len(passing), len(tickers)
        )
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (3, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["FundamentalScreen"]
