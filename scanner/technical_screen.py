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

        high_52w = close.rolling(252).max().iloc[-1]
        low_52w = close.rolling(252).min().iloc[-1]

        if any(pd.isna(v) for v in [last_ma50, last_ma150, last_ma200, ma200_month_ago]):
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

    def screen(self, tickers: list[str]) -> list[str]:
        """Run Stage 5 technical screening. Returns Stage 2 breakout candidates."""
        # Get SPY 1-year return for RS computation
        spy_return = 0.0
        try:
            spy_df = self._get_ohlcv(_RS_BENCHMARK, period="1y")
            if spy_df is not None and len(spy_df) > 1:
                spy_return = (spy_df["close"].iloc[-1] / spy_df["close"].iloc[0] - 1) * 100
        except Exception as exc:
            logger.warning("TechnicalScreen: SPY fetch failed: %s", exc)

        passing: list[str] = []
        for ticker in tickers:
            try:
                df = self._get_ohlcv(ticker)
                if df is None:
                    continue

                stage2 = self._is_stage2(df)
                in_base = self._is_in_base(df)
                rs = self._compute_rs_rank(ticker, spy_return)

                if stage2 and in_base and rs >= _MIN_RS_RANK:
                    passing.append(ticker)
                    logger.debug(
                        "Stage5 PASS %s stage2=%s base=%s rs=%.1f",
                        ticker, stage2, in_base, rs,
                    )
            except Exception as exc:
                logger.debug("Stage5: error for %s: %s", ticker, exc)

        logger.info("Stage 5 technical screen: %d/%d pass", len(passing), len(tickers))
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (5, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["TechnicalScreen"]
