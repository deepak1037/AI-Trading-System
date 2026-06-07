"""Signal accuracy logger — daily fill-in of predicted vs actual (Day 5).

At end of day, computes whether each intraday signal was correct by
comparing the predicted direction to the actual SPY/NQ % move.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class AccuracyLogger:
    """Logs and evaluates signal accuracy for model retraining.

    Daily EOD job:
      1. Fetch actual % move for the day (from yfinance)
      2. Map move to direction (long/short/neutral)
      3. Compare to each signal's predicted direction
      4. Update accuracy_log.was_correct
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH

    def log_prediction(
        self,
        signal_id: int,
        predicted_direction: str,
        log_date: Optional[str] = None,
    ) -> int:
        """Create an accuracy_log row at signal time (actual filled EOD)."""
        log_date = log_date or date.today().isoformat()
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO accuracy_log
                   (signal_id, predicted_direction, date)
                   VALUES (?, ?, ?)""",
                (signal_id, predicted_direction, log_date),
            )
            return cur.lastrowid or 0

    def fill_actual(
        self,
        log_date: Optional[str] = None,
        benchmark_ticker: str = "SPY",
    ) -> int:
        """EOD job: compute actual direction and update was_correct.

        Returns the number of rows updated.
        """
        log_date = log_date or date.today().isoformat()
        actual_direction, actual_pct = self._get_actual_direction(
            log_date, benchmark_ticker
        )
        logger.info(
            "AccuracyLogger EOD fill: date=%s ticker=%s pct=%.3f direction=%s",
            log_date, benchmark_ticker, actual_pct, actual_direction,
        )

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, predicted_direction FROM accuracy_log WHERE date=? AND was_correct IS NULL",
                (log_date,),
            ).fetchall()

            updated = 0
            for row_id, predicted in rows:
                was_correct = int(
                    self._directions_agree(predicted, actual_direction)
                )
                conn.execute(
                    """UPDATE accuracy_log
                       SET actual_direction=?, actual_pct_move=?, was_correct=?
                       WHERE id=?""",
                    (actual_direction, actual_pct, was_correct, row_id),
                )
                updated += 1

        logger.info("AccuracyLogger: updated %d rows for %s", updated, log_date)
        return updated

    def _get_actual_direction(
        self, log_date: str, ticker: str = "SPY"
    ) -> tuple[str, float]:
        """Fetch the actual % move for ticker on log_date."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            data = yf.Ticker(ticker).history(start=log_date, end=log_date, interval="1d")
            if data.empty:
                logger.warning("No price data for %s on %s", ticker, log_date)
                return "neutral", 0.0
            open_ = float(data["Open"].iloc[0])
            close = float(data["Close"].iloc[0])
            if open_ == 0:
                return "neutral", 0.0
            pct = (close - open_) / open_
            if pct >= 0.005:
                return "long", pct
            elif pct <= -0.005:
                return "short", pct
            return "neutral", pct
        except Exception as exc:
            logger.warning("AccuracyLogger: failed to fetch actual for %s: %s", ticker, exc)
            return "neutral", 0.0

    @staticmethod
    def _directions_agree(predicted: str, actual: str) -> bool:
        """True if predicted and actual are in the same directional bucket."""
        bullish = {"long", "strong_long"}
        bearish = {"short", "strong_short"}
        if predicted in bullish and actual in bullish:
            return True
        if predicted in bearish and actual in bearish:
            return True
        if predicted == "neutral" and actual == "neutral":
            return True
        return False

    def get_accuracy(self, days: int = 30) -> dict:
        """Return accuracy statistics for the last N days."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT predicted_direction, actual_direction, was_correct
                   FROM accuracy_log
                   WHERE was_correct IS NOT NULL
                     AND date >= date('now', ? || ' days')""",
                (f"-{days}",),
            ).fetchall()

        if not rows:
            return {"total": 0, "correct": 0, "accuracy_pct": 0.0}

        total = len(rows)
        correct = sum(1 for r in rows if r[2] == 1)
        return {
            "total": total,
            "correct": correct,
            "accuracy_pct": round(correct / total * 100, 1),
            "days_window": days,
        }


__all__ = ["AccuracyLogger"]
