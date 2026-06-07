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

_FMP_BASE = "https://financialmodelingprep.com/api/v3"


class FundamentalScreen:
    """Stage 3: EPS growth + revenue growth + estimate revisions."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._fmp_key = settings.FMP_API_KEY
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "AI-Trading-System/1.0"
        if not self._fmp_key:
            logger.warning(
                "FMP_API_KEY not set — Stage 3 using simplified yfinance .info "
                "filter with lenient thresholds. Set FMP_API_KEY in .env for the "
                "full EPS/revenue/estimate-revision screen."
            )

    # ── FMP path ─────────────────────────────────────────────────────────────
    def _fmp_get(self, path: str, **params) -> Any:
        params["apikey"] = self._fmp_key
        try:
            resp = self._session.get(
                f"{_FMP_BASE}/{path}", params=params, timeout=15
            )
            if resp.status_code == 429:
                logger.warning("FMP rate limited on %s — backing off", path)
                time.sleep(2.0)
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("FMP %s failed: %s", path, exc)
            return None

    def _passes_fmp(self, ticker: str) -> bool:
        """Pass condition (OR logic) using FMP quarterly fundamentals.

          - EPS accelerating (latest quarter beats prior) OR growing YoY
          - Revenue growing YoY (>5%)
          - Positive analyst estimate revisions (best-effort bonus)
        """
        income = self._fmp_get(
            f"income-statement/{ticker}", period="quarter", limit=8
        )
        if not isinstance(income, list) or len(income) < 2:
            return False

        eps = [self._num(q.get("epsdiluted", q.get("eps"))) for q in income]
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
        est_ok = self._fmp_estimates_positive(ticker)

        passed = eps_qoq or eps_yoy or rev_yoy or est_ok
        logger.debug(
            "Stage3 FMP %s: eps_qoq=%s eps_yoy=%s rev_yoy=%s est_ok=%s -> %s",
            ticker, eps_qoq, eps_yoy, rev_yoy, est_ok, passed,
        )
        return passed

    def _fmp_estimates_positive(self, ticker: str) -> bool:
        """Best-effort: analyst estimating rising EPS next quarter vs current."""
        data = self._fmp_get(
            f"analyst-estimates/{ticker}", period="quarter", limit=2
        )
        if not isinstance(data, list) or len(data) < 2:
            return False
        # FMP returns most-recent first; compare next-period estimate to current.
        nxt = self._num(data[0].get("estimatedEpsAvg"))
        cur = self._num(data[1].get("estimatedEpsAvg"))
        return nxt is not None and cur is not None and cur != 0 and nxt > cur

    # ── Fallback path (yfinance .info, no earnings calendar) ─────────────────
    def _passes_fallback(self, ticker: str) -> bool:
        """Lenient filter from yfinance ``.info`` — no earnings calendar.

        A name passes if it shows *any* of: positive earnings, positive revenue
        growth, positive earnings growth, or a healthy gross margin. Thresholds
        are deliberately loose so a reasonable slice (~50–100 of a few hundred)
        survives without the richer FMP data.
        """
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Stage3 fallback: .info failed for %s: %s", ticker, exc)
            return False
        if not info:
            return False

        trailing_eps = self._num(info.get("trailingEps"))
        rev_growth = self._num(info.get("revenueGrowth"))
        earn_growth = self._num(info.get("earningsGrowth")) or self._num(
            info.get("earningsQuarterlyGrowth")
        )
        gross_margin = self._num(info.get("grossMargins"))

        profitable = trailing_eps is not None and trailing_eps > 0
        growing_rev = rev_growth is not None and rev_growth > 0
        growing_earn = earn_growth is not None and earn_growth > 0
        healthy_margin = gross_margin is not None and gross_margin > 0.25

        # Lenient OR: any genuine sign of fundamental health.
        passed = (
            (profitable and growing_rev)
            or growing_earn
            or (growing_rev and healthy_margin)
            or (rev_growth is not None and rev_growth > 0.10)
        )
        logger.debug(
            "Stage3 fallback %s: eps=%s revG=%s earnG=%s gm=%s -> %s",
            ticker, trailing_eps, rev_growth, earn_growth, gross_margin, passed,
        )
        return passed

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
    def screen(self, tickers: list[str]) -> list[str]:
        """Run Stage 3 screening. Returns tickers that pass fundamental criteria."""
        use_fmp = bool(self._fmp_key)
        pace = settings.SCANNER_YF_PACE_SECONDS
        passing: list[str] = []
        for ticker in tickers:
            if pace and not use_fmp:  # only the yfinance .info path needs pacing
                time.sleep(pace)
            try:
                ok = self._passes_fmp(ticker) if use_fmp else self._passes_fallback(ticker)
                if ok:
                    passing.append(ticker)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Stage3: error for %s: %s", ticker, exc)

        mode = "FMP" if use_fmp else "yfinance-fallback"
        logger.info(
            "Stage 3 fundamental screen (%s): %d/%d pass", mode, len(passing), len(tickers)
        )
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (3, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["FundamentalScreen"]
