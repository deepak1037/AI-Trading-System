"""Scanner Stage 3: Fundamental inflection screen (Days 13-18).

Filters ~1,800 liquidity-passing stocks to ~400 by:
  - EPS acceleration for 2+ consecutive quarters
  - Revenue re-acceleration (at least 1 quarter of re-accel after slowdown)
  - Earnings estimate revisions > 75% upward

Data source: yfinance (free) with optional FMP upgrade
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class FundamentalScreen:
    """Stage 3: EPS acceleration + revenue re-accel + estimate revisions."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH

    def _get_quarterly_earnings(self, ticker: str) -> list[dict]:
        """Fetch quarterly EPS history from yfinance."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            df = t.quarterly_earnings
            if df is None or df.empty:
                return []
            records = []
            for idx, row in df.iterrows():
                records.append({
                    "period": str(idx),
                    "actual": float(row.get("Earnings", 0) or 0),
                    "estimate": float(row.get("Estimated", 0) or 0),
                })
            return records[-8:]  # last 8 quarters
        except Exception as exc:
            logger.debug("FundamentalScreen: earnings fetch failed for %s: %s", ticker, exc)
            return []

    def _get_quarterly_revenue(self, ticker: str) -> list[float]:
        """Fetch quarterly revenue from yfinance income statement."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            fin = t.quarterly_income_stmt
            if fin is None or fin.empty:
                return []
            rev_row = None
            for row_name in ["Total Revenue", "Revenue", "TotalRevenue"]:
                if row_name in fin.index:
                    rev_row = fin.loc[row_name]
                    break
            if rev_row is None:
                return []
            return [float(v) for v in rev_row.values[:8] if v is not None]
        except Exception as exc:
            logger.debug("FundamentalScreen: revenue fetch failed for %s: %s", ticker, exc)
            return []

    def _eps_accelerating(self, earnings: list[dict], min_quarters: int = 2) -> bool:
        """Return True if EPS growth is accelerating for min_quarters consecutive qtrs."""
        if len(earnings) < min_quarters + 2:
            return False
        actuals = [e["actual"] for e in earnings]
        # Compute YoY growth per quarter (quarter n vs quarter n-4)
        if len(actuals) < 5:
            return False
        growths = []
        for i in range(len(actuals) - 4):
            prior = actuals[i + 4]
            current = actuals[i]
            if prior != 0:
                growths.append((current - prior) / abs(prior))
        if len(growths) < min_quarters:
            return False
        # Check last min_quarters are accelerating
        for i in range(min_quarters - 1):
            if growths[i] <= growths[i + 1]:
                return False
        return True

    def _revenue_reaccelerating(self, revenues: list[float]) -> bool:
        """Return True if revenue growth re-accelerated after a period of slowdown."""
        if len(revenues) < 5:
            return False
        # Compute QoQ growth rates
        growths = []
        for i in range(len(revenues) - 1):
            prior = revenues[i + 1]
            if prior > 0:
                growths.append((revenues[i] - prior) / prior)
        if len(growths) < 3:
            return False
        # Re-accel: last growth > previous growth (growth is increasing again)
        return growths[0] > growths[1]

    def _estimate_revisions_positive(self, earnings: list[dict]) -> bool:
        """Return True if recent revisions are upward (actual > estimate 75%+ of time)."""
        valid = [e for e in earnings if e["estimate"] != 0]
        if not valid:
            return False
        beats = sum(1 for e in valid if e["actual"] > e["estimate"])
        return (beats / len(valid)) >= 0.75

    def screen(self, tickers: list[str]) -> list[str]:
        """Run Stage 3 screening. Returns tickers that pass fundamental criteria."""
        passing: list[str] = []

        for ticker in tickers:
            try:
                earnings = self._get_quarterly_earnings(ticker)
                revenues = self._get_quarterly_revenue(ticker)

                eps_ok = self._eps_accelerating(earnings)
                rev_ok = self._revenue_reaccelerating(revenues)
                est_ok = self._estimate_revisions_positive(earnings)

                if eps_ok or (rev_ok and est_ok):
                    passing.append(ticker)
                    logger.debug(
                        "Stage3 PASS %s eps_accel=%s rev_reaccel=%s est_up=%s",
                        ticker, eps_ok, rev_ok, est_ok,
                    )
            except Exception as exc:
                logger.debug("Stage3: error for %s: %s", ticker, exc)

        logger.info("Stage 3 fundamental screen: %d/%d pass", len(passing), len(tickers))
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (3, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["FundamentalScreen"]
