"""Signal accuracy tracker — records and reports which signals led to wins.

After a trade closes, track_signal() logs whether the market signal was
directionally correct. generate_report() aggregates accuracy per source,
per confidence tier, and per direction, then recommends the optimal
confidence threshold.

Requires MIN_SIGNALS_FOR_ACCURACY samples before reporting is meaningful.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger
from data.db import get_connection

logger = get_logger(__name__)

_SOURCES = (
    "macro", "yield", "sentiment", "premarket",
    "technical", "fusion", "presidential", "geopolitical",
)


class SignalAccuracyReport(BaseModel):
    period: str
    insufficient_data: bool = False
    total_signals: int

    # Per-source accuracy (0.0–1.0)
    source_accuracy: dict[str, float]
    best_signal_source: str
    worst_signal_source: str

    # Confidence-tier accuracy
    high_confidence_accuracy: float   # confidence > 65
    critical_accuracy: float          # confidence > 80

    # Direction accuracy
    long_accuracy: float
    short_accuracy: float
    neutral_accuracy: float

    # Optimal threshold (confidence level that maximises accuracy)
    optimal_confidence_threshold: int


class SignalAccuracyTracker:
    """Records signal outcomes and reports accuracy statistics."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.DB_PATH

    # ── write ─────────────────────────────────────────────────────────────────

    def track_signal(
        self,
        signal_date: str,
        direction: str,
        confidence: int,
        composite_score: int,
        sources: list[str],
        trade_id: int | None,
        was_correct: bool,
        actual_move_pct: float | None = None,
    ) -> None:
        """Log a signal outcome to the signal_accuracy table."""
        try:
            with get_connection(self._db_path) as conn:
                conn.execute(
                    """INSERT INTO signal_accuracy
                       (signal_date, direction, confidence, composite_score,
                        sources, trade_id, was_correct, actual_move_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        signal_date,
                        direction,
                        confidence,
                        composite_score,
                        json.dumps(sources),
                        trade_id,
                        int(was_correct),
                        actual_move_pct,
                    ),
                )
        except Exception as exc:
            logger.warning("SignalAccuracyTracker: write failed: %s", exc)

    # ── read ──────────────────────────────────────────────────────────────────

    def generate_report(self, period: str = "MTD") -> SignalAccuracyReport:
        """Aggregate signal accuracy for the given period."""
        start = self._period_start(period)
        rows = self._load(start)

        if len(rows) < settings.MIN_SIGNALS_FOR_ACCURACY:
            logger.info(
                "SignalAccuracyTracker: only %d signal(s) — minimum is %d",
                len(rows), settings.MIN_SIGNALS_FOR_ACCURACY,
            )
            return self._empty_report(period)

        source_acc = self._source_accuracy(rows)
        best = max(source_acc, key=lambda s: source_acc[s], default="")
        worst = min(source_acc, key=lambda s: source_acc[s], default="")

        high_conf = [r for r in rows if r["confidence"] and r["confidence"] > 65]
        critical = [r for r in rows if r["confidence"] and r["confidence"] > 80]
        longs = [r for r in rows if r["direction"] in ("long", "strong_long")]
        shorts = [r for r in rows if r["direction"] in ("short", "strong_short")]
        neutrals = [r for r in rows if r["direction"] == "neutral"]

        return SignalAccuracyReport(
            period=period,
            insufficient_data=False,
            total_signals=len(rows),
            source_accuracy=source_acc,
            best_signal_source=best,
            worst_signal_source=worst,
            high_confidence_accuracy=round(self._acc(high_conf), 4),
            critical_accuracy=round(self._acc(critical), 4),
            long_accuracy=round(self._acc(longs), 4),
            short_accuracy=round(self._acc(shorts), 4),
            neutral_accuracy=round(self._acc(neutrals), 4),
            optimal_confidence_threshold=self._optimal_threshold(rows),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _load(self, start: date) -> list[dict]:
        try:
            with get_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT direction, confidence, composite_score, sources, "
                    "was_correct FROM signal_accuracy WHERE signal_date >= ?",
                    (start.isoformat(),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("SignalAccuracyTracker: load failed: %s", exc)
            return []

    def _source_accuracy(self, rows: list[dict]) -> dict[str, float]:
        per_source: dict[str, list[int]] = {s: [] for s in _SOURCES}
        for row in rows:
            try:
                sources = json.loads(row.get("sources") or "[]")
            except Exception:
                sources = []
            for src in sources:
                if src in per_source:
                    per_source[src].append(int(row.get("was_correct") or 0))
        return {s: round(sum(v) / len(v), 4) for s, v in per_source.items() if v}

    @staticmethod
    def _acc(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        return sum(int(r.get("was_correct") or 0) for r in rows) / len(rows)

    @staticmethod
    def _optimal_threshold(rows: list[dict]) -> int:
        best_threshold = 50
        best_acc = 0.0
        for threshold in range(40, 95, 5):
            subset = [r for r in rows if r.get("confidence") and r["confidence"] >= threshold]
            if len(subset) < 1:
                continue
            acc = sum(int(r.get("was_correct") or 0) for r in subset) / len(subset)
            if acc > best_acc:
                best_acc = acc
                best_threshold = threshold
        return best_threshold

    @staticmethod
    def _period_start(period: str) -> date:
        today = date.today()
        if period == "MTD":
            return today.replace(day=1)
        if period == "QTD":
            month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=month, day=1)
        if period == "YTD":
            return today.replace(month=1, day=1)
        return today - timedelta(days=30)

    @staticmethod
    def _empty_report(period: str) -> SignalAccuracyReport:
        return SignalAccuracyReport(
            period=period,
            insufficient_data=True,
            total_signals=0,
            source_accuracy={},
            best_signal_source="",
            worst_signal_source="",
            high_confidence_accuracy=0.0,
            critical_accuracy=0.0,
            long_accuracy=0.0,
            short_accuracy=0.0,
            neutral_accuracy=0.0,
            optimal_confidence_threshold=65,
        )


__all__ = ["SignalAccuracyTracker", "SignalAccuracyReport"]
