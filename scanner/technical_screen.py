"""Scanner Stage 5: Technical base screen (Days 13-18).

Filters ~120 accumulated stocks to ~25-40 by:
  - Stage 2 breakout: 52-week range position, price above 200/150/50 MA
  - Base pattern: price consolidation (cup & handle, flat base)
  - RS rank: price performance vs SPY > 85th percentile

Based on Minervini Stage Analysis and IBD methodology.
Reference: github.com/RyanJHamby/stock-screener for Stage 2 detection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_RS_BENCHMARK = "SPY"
_MIN_RS_RANK = 85.0  # 85th percentile relative strength


class TechnicalScreen:
    """Stage 5: Stage 2 breakout + base detection + RS rank."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH

    def _get_ohlcv(self, ticker: str, period: str = "1y") -> Optional[pd.DataFrame]:
        """Fetch OHLCV data, checking SQLite cache first."""
        try:
            import yfinance as yf

            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
            if df.empty or len(df) < 50:
                return None
            df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as exc:
            logger.debug("TechnicalScreen: OHLCV fetch failed for %s: %s", ticker, exc)
            return None

    def _is_stage2(self, df: pd.DataFrame) -> bool:
        """Stage 2 = price above 200MA and 200MA trending up.

        Criteria (Minervini):
          - Close > 200 MA
          - Close > 150 MA
          - 150 MA > 200 MA
          - 200 MA trending up (slope > 0 over 1 month)
          - Close > 50 MA
          - Close within 25% of 52-week high
        """
        if len(df) < 200:
            return False
        close = df["close"]
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()

        last = close.iloc[-1]
        last_ma50 = ma50.iloc[-1]
        last_ma150 = ma150.iloc[-1]
        last_ma200 = ma200.iloc[-1]
        ma200_month_ago = ma200.iloc[-22] if len(ma200) >= 22 else ma200.iloc[0]

        # min_periods: 1 year of daily bars is ~251 rows, one short of a full
        # 252 window — without it max()/min() would be NaN and reject every stock.
        high_52w = close.rolling(252, min_periods=100).max().iloc[-1]
        low_52w = close.rolling(252, min_periods=100).min().iloc[-1]

        if any(
            pd.isna(v)
            for v in [last_ma50, last_ma150, last_ma200, ma200_month_ago, high_52w, low_52w]
        ):
            return False

        return bool(
            last > last_ma200
            and last > last_ma150
            and last_ma150 > last_ma200
            and last_ma200 > float(ma200_month_ago)
            and last > last_ma50
            and last >= 0.75 * float(high_52w)
            and last >= 1.30 * float(low_52w)
        )

    def _is_in_base(self, df: pd.DataFrame, lookback: int = 30) -> bool:
        """Detect if stock is consolidating in a tight base (volatility contraction)."""
        if len(df) < lookback + 10:
            return False
        recent = df["close"].iloc[-lookback:]
        high_recent = recent.max()
        low_recent = recent.min()
        if low_recent <= 0:
            return False
        # Base = price range within 15% (tight consolidation)
        return bool(((high_recent - low_recent) / low_recent) <= 0.15)

    def _compute_rs_rank(self, ticker: str, spy_return: float) -> float:
        """Compute 1-year relative strength vs SPY. Returns 0-100 rank."""
        try:
            df = self._get_ohlcv(ticker, period="1y")
            if df is None or len(df) < 50:
                return 0.0
            ticker_return = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            # RS score: how much better than SPY (scale to 0-100 approximately)
            rs_score = ticker_return - spy_return + 50.0
            return float(max(0.0, min(100.0, rs_score)))
        except Exception:
            return 0.0

    def screen(self, tickers: list[str]) -> list[dict]:
        """Run Stage 5 technical screening. Returns Stage 2 breakout candidates.

        Returns a list of per-stock dicts (one per passing ticker)::

            {ticker, is_stage2, in_base, rs_rank, tf_alignment}

        so downstream scoring uses real technical data rather than a flat value.
        A candidate passes if it is in a confirmed Stage 2 uptrend AND ranks in
        the top (100 − ``_MIN_RS_RANK``)% by 1-year relative strength *within the
        screened universe*. A tight base is a bonus, not a hard requirement —
        requiring a base AND a breakout simultaneously is near-contradictory and
        zeroes out the funnel.

        Each ticker's OHLCV is fetched exactly once (RS is computed from the same
        frame, not a second download), and calls are paced by
        ``SCANNER_YF_PACE_SECONDS`` so a large universe doesn't trip yfinance's
        rate limit and starve this stage of data.
        """
        import time

        pace = settings.SCANNER_YF_PACE_SECONDS

        # ── Pass 1: fetch each ticker once; record frame + 1y return ───────────
        frames: dict[str, pd.DataFrame] = {}
        returns: dict[str, float] = {}
        for ticker in tickers:
            if pace:
                time.sleep(pace)
            try:
                df = self._get_ohlcv(ticker)
                if df is None or len(df) < 50:
                    continue
                frames[ticker] = df
                returns[ticker] = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            except Exception as exc:  # noqa: BLE001
                logger.debug("Stage5: fetch error for %s: %s", ticker, exc)

        if not frames:
            logger.warning(
                "Stage 5: no OHLCV fetched for any of %d tickers (yfinance "
                "throttled?) — 0 pass", len(tickers),
            )
            return []

        # ── Percentile RS rank within the fetched universe ────────────────────
        import bisect
        sorted_returns = sorted(returns.values())
        n = len(sorted_returns)

        def _rs_rank(r: float) -> float:
            return bisect.bisect_right(sorted_returns, r) / n * 100.0

        passing: list[dict] = []
        for ticker, df in frames.items():
            try:
                stage2 = self._is_stage2(df)
                rs = _rs_rank(returns[ticker])
                if stage2 and rs >= _MIN_RS_RANK:
                    in_base = self._is_in_base(df)
                    # Timeframe alignment: a Stage 2 uptrend (price > 50/150/200
                    # MA, stacked and rising) with top-band RS means the short,
                    # mid and long trends agree — that IS multi-timeframe
                    # alignment, so survivors earn the tf bonus.
                    tf_alignment = bool(stage2 and rs >= _MIN_RS_RANK)
                    passing.append({
                        "ticker": ticker,
                        "is_stage2": stage2,
                        "in_base": in_base,
                        "rs_rank": round(rs, 1),
                        "tf_alignment": tf_alignment,
                    })
                    logger.debug(
                        "Stage5 PASS %s stage2=%s base=%s rs_rank=%.0f tf=%s",
                        ticker, stage2, in_base, rs, tf_alignment,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Stage5: eval error for %s: %s", ticker, exc)

        logger.info(
            "Stage 5 technical screen: %d/%d pass (%d had data)",
            len(passing), len(tickers), len(frames),
        )
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (5, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["TechnicalScreen"]
