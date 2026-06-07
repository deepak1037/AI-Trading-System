"""Scanner Stage 3: Fundamental inflection screen (Days 13-18).

Filters ~1,800 liquidity-passing stocks to ~400 by:
  - EPS acceleration for 2+ consecutive quarters
  - Revenue re-acceleration (at least 1 quarter of re-accel after slowdown)
  - Earnings estimate revisions > 75% upward (beats rate)

Data source: yfinance (free) — uses earnings_dates for EPS/estimates,
quarterly_income_stmt for revenue.
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

    def _get_earnings_data(self, ticker: str) -> list[dict]:
        """Fetch earnings history via earnings_dates (replaces deprecated quarterly_earnings).

        Returns list of {period, actual, estimate} dicts, most recent first.
        """
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            df = t.earnings_dates
            if df is None or df.empty:
                return []

            records = []
            for idx, row in df.iterrows():
                actual = row.get("Reported EPS")
                estimate = row.get("EPS Estimate")
                if actual is None and estimate is None:
                    continue
                try:
                    records.append({
                        "period": str(idx),
                        "actual": float(actual) if actual is not None else 0.0,
                        "estimate": float(estimate) if estimate is not None else 0.0,
                    })
                except (TypeError, ValueError):
                    continue
            return records[:8]  # last 8 quarters, most recent first
        except Exception as exc:
            logger.debug("FundamentalScreen: earnings_dates fetch failed for %s: %s", ticker, exc)
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
            for row_name in ("Total Revenue", "Revenue", "TotalRevenue"):
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
        """Return True if EPS beats are improving (actual > estimate) for min_quarters."""
        # With earnings_dates, actuals are in chronological order (most recent = index 0)
        # Check that recent quarters are positive surprises and beat rate is high
        valid = [e for e in earnings if e["estimate"] != 0]
        if len(valid) < min_quarters:
            return False

        # Check last N quarters all beat
        recent = valid[:min_quarters]
        return all(e["actual"] > e["estimate"] for e in recent)

    def _eps_yoy_accelerating(self, earnings: list[dict]) -> bool:
        """Return True if EPS growth YoY is accelerating (need 8 quarters)."""
        actuals = [e["actual"] for e in earnings]
        if len(actuals) < 6:
            return False
        # Compare Q0 vs Q4 (YoY) and Q1 vs Q5 (YoY) — check recent is better
        try:
            growth_recent = (actuals[0] - actuals[4]) / abs(actuals[4]) if actuals[4] != 0 else 0
            growth_prior = (actuals[1] - actuals[5]) / abs(actuals[5]) if actuals[5] != 0 else 0
            return growth_recent > growth_prior
        except Exception:
            return False

    def _revenue_reaccelerating(self, revenues: list[float]) -> bool:
        """Return True if revenue growth re-accelerated after a period of slowdown."""
        if len(revenues) < 4:
            return False
        # revenues[0] = most recent quarter (yfinance orders newest first)
        # Compute QoQ growth for recent 3 quarters
        growths = []
        for i in range(min(len(revenues) - 1, 4)):
            prior = revenues[i + 1]
            if prior > 0:
                growths.append((revenues[i] - prior) / prior)
        if len(growths) < 2:
            return False
        # Re-accel: most recent growth > prior growth
        return growths[0] > growths[1]

    def _revenue_yoy_growing(self, revenues: list[float]) -> bool:
        """Return True if revenue is growing YoY."""
        if len(revenues) < 5:
            return False
        # Compare most recent quarter to same quarter a year ago
        try:
            return revenues[0] > revenues[4] * 1.05  # 5% YoY growth floor
        except Exception:
            return False

    def _estimate_revisions_positive(self, earnings: list[dict]) -> bool:
        """Return True if EPS beat rate >= 75% in recent quarters."""
        valid = [e for e in earnings if e["estimate"] != 0 and e["actual"] != 0]
        if not valid:
            return False
        beats = sum(1 for e in valid if e["actual"] > e["estimate"])
        return (beats / len(valid)) >= 0.75

    def screen(self, tickers: list[str]) -> list[str]:
        """Run Stage 3 screening. Returns tickers that pass fundamental criteria.

        Pass condition (OR logic — any one is enough):
          - EPS accelerating (recent quarters beating estimates)
          - Revenue re-accelerating AND strong beat rate (> 75%)
          - EPS YoY accelerating AND revenue growing YoY
        """
        passing: list[str] = []

        for ticker in tickers:
            try:
                earnings = self._get_earnings_data(ticker)
                revenues = self._get_quarterly_revenue(ticker)

                eps_beats = self._eps_accelerating(earnings)
                rev_reaccel = self._revenue_reaccelerating(revenues)
                est_ok = self._estimate_revisions_positive(earnings)
                eps_yoy = self._eps_yoy_accelerating(earnings)
                rev_yoy = self._revenue_yoy_growing(revenues)

                if eps_beats or (rev_reaccel and est_ok) or (eps_yoy and rev_yoy):
                    passing.append(ticker)
                    logger.debug(
                        "Stage3 PASS %s eps_beats=%s rev_reaccel=%s est_ok=%s eps_yoy=%s rev_yoy=%s",
                        ticker, eps_beats, rev_reaccel, est_ok, eps_yoy, rev_yoy,
                    )
                else:
                    logger.debug(
                        "Stage3 FAIL %s eps_beats=%s rev_reaccel=%s est_ok=%s eps_yoy=%s rev_yoy=%s",
                        ticker, eps_beats, rev_reaccel, est_ok, eps_yoy, rev_yoy,
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
