"""Bucket classification, capacity checks, and per-bucket P&L (Phase 2 Step 2).

The three-bucket framework routes every position into one of three risk
buckets, each with its own capital allocation and exit discipline. This module
is the single place that decides *which* bucket a strategy belongs to.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# ── Strategy → bucket mapping ────────────────────────────────────────────────
# Bucket 1: income engine — short premium we can roll/recover.
_BUCKET1_MSP = {"cash_secured_put", "margin_secured_put", "msp"}
_BUCKET1_WHEEL_CALL = {"covered_call", "wheel_call"}
# Bucket 2A: earnings puts (recoverable). 2B: defined-risk spreads (not).
_BUCKET2A = {"earnings_put"}
_BUCKET2B = {
    "call_spread",
    "put_spread",
    "iron_condor",
    "straddle",
    "strangle",
    "earnings_spread",
}
# Bucket 3: directional long options (binary outcome).
_BUCKET3_LONG = {"long_call", "long_put", "event_call", "event_put", "leap"}

# DTE boundary between a LEAP and a shorter-dated event play (Step 2 spec).
_LEAP_DTE_THRESHOLD = 90


class BucketClassification(BaseModel):
    """The result of classifying a single position."""

    bucket: int            # 1, 2, or 3
    sub_type: str
    recoverable: bool
    description: str
    exit_rules: str        # human-readable exit-rule summary


class BucketPnL(BaseModel):
    """Aggregated performance for one bucket."""

    bucket: int
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    premium_collected: float = 0.0
    win_rate: float = 0.0
    open_positions: int = 0
    mtd_return: float = 0.0


def _get(pos: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict (DB row) or an attribute-bearing object."""
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


# Per-bucket one-line exit-rule summaries (shown in the dashboard/alerts).
_EXIT_SUMMARY = {
    1: "Take 50% profit / 21-DTE exit / always ROLL, never stop-loss",
    "2a": "As Bucket 1, plus exit before earnings (IV-crush trade)",
    "2b": "50% profit OR 2x-premium stop-loss / 21-DTE / never roll",
    3: "Exit at profit target or expiry — binary, never roll",
}


class BucketManager:
    """Classifies positions and reports per-bucket capacity and P&L."""

    def __init__(
        self,
        db_path: str | None = None,
        paper_account: Any = None,
    ) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._paper_account = paper_account

    # ── classification ───────────────────────────────────────────────────────
    def classify_position(self, position: Any) -> BucketClassification:
        """Map a position to its bucket / sub-type / recoverability.

        ``position`` may be a dict (positions row), a ``BucketPosition``, or any
        object exposing ``strategy``/``sub_type`` and ``dte_remaining`` fields.
        """
        strategy = (
            _get(position, "strategy")
            or _get(position, "strategy_name")
            or _get(position, "sub_type")
            or ""
        ).lower()
        option_type = (_get(position, "option_type") or "").lower()
        dte = int(
            _get(position, "dte_remaining", _get(position, "days_to_expiry", 0)) or 0
        )

        if strategy in _BUCKET1_MSP:
            return self._mk(1, "msp", True)
        if strategy in _BUCKET1_WHEEL_CALL:
            return self._mk(1, "wheel_call", True)
        if strategy in _BUCKET2A:
            return self._mk(2, "earnings_put", True, key="2a")
        if strategy in _BUCKET2B:
            return self._mk(2, "earnings_spread", False, key="2b")
        if strategy in _BUCKET3_LONG:
            if "leap" in strategy or dte > _LEAP_DTE_THRESHOLD:
                return self._mk(3, "leap", False)
            sub = "event_put" if ("put" in strategy or option_type == "put") else "event_call"
            return self._mk(3, sub, False)

        # Unknown strategy — default to the income bucket, flagged recoverable.
        logger.warning("BucketManager: unclassified strategy '%s' → default B1", strategy)
        return self._mk(1, "msp", True, description=f"unclassified strategy '{strategy}'")

    def _mk(
        self,
        bucket: int,
        sub_type: str,
        recoverable: bool,
        key: Any = None,
        description: str | None = None,
    ) -> BucketClassification:
        names = {1: "MSP/Wheel", 2: "Earnings", 3: "Event/LEAP"}
        return BucketClassification(
            bucket=bucket,
            sub_type=sub_type,
            recoverable=recoverable,
            description=description or f"Bucket {bucket} — {names[bucket]} ({sub_type})",
            exit_rules=_EXIT_SUMMARY[key if key is not None else bucket],
        )

    # ── queries ──────────────────────────────────────────────────────────────
    def get_bucket_positions(self, bucket_num: int) -> list[dict]:
        """Open positions in a bucket (from the positions table)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM positions WHERE bucket=? AND is_open=1",
                (bucket_num,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _bucket_exposure(self, bucket_num: int) -> float:
        """Current capital deployed in a bucket: sum(|entry_price| × |qty|)."""
        total = 0.0
        for row in self.get_bucket_positions(bucket_num):
            total += abs(row.get("entry_price") or 0.0) * abs(row.get("qty") or 0)
        return total

    def _equity(self) -> float:
        if self._paper_account is None:
            return 0.0
        try:
            return float(self._paper_account.get_state().equity)
        except Exception as exc:
            logger.debug("BucketManager: equity lookup failed: %s", exc)
            return 0.0

    def _allocation_pct(self, bucket_num: int) -> int:
        return {
            1: settings.BUCKET1_ALLOCATION_PCT,
            2: settings.BUCKET2_ALLOCATION_PCT,
            3: settings.BUCKET3_ALLOCATION_PCT,
        }.get(bucket_num, 0)

    def check_bucket_capacity(self, bucket: int, new_position_value: float) -> bool:
        """True if adding ``new_position_value`` keeps the bucket within budget."""
        equity = self._equity()
        if equity <= 0:
            # No equity context → cannot enforce; allow but warn.
            logger.warning("BucketManager: no equity context — capacity check skipped")
            return True
        bucket_max = equity * (self._allocation_pct(bucket) / 100.0)
        current = self._bucket_exposure(bucket)
        ok = (current + new_position_value) <= bucket_max
        logger.debug(
            "BucketManager: capacity B%d current=%.0f new=%.0f max=%.0f → %s",
            bucket, current, new_position_value, bucket_max, ok,
        )
        return ok

    def get_bucket_pnl(self, bucket_num: int) -> BucketPnL:
        """Realized/win-rate/open-count for a bucket (+ premium from logs)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            closed = conn.execute(
                "SELECT realized_pnl FROM positions "
                "WHERE bucket=? AND is_open=0 AND realized_pnl IS NOT NULL",
                (bucket_num,),
            ).fetchall()
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE bucket=? AND is_open=1",
                (bucket_num,),
            ).fetchone()["n"]
            premium_row = conn.execute(
                "SELECT COALESCE(SUM(premium_collected), 0.0) AS p "
                "FROM bucket_performance WHERE bucket=?",
                (bucket_num,),
            ).fetchone()

        pnls = [r["realized_pnl"] for r in closed]
        realized = float(sum(pnls))
        wins = sum(1 for p in pnls if p > 0)
        win_rate = (wins / len(pnls) * 100.0) if pnls else 0.0
        equity = self._equity()
        mtd = (realized / equity * 100.0) if equity > 0 else 0.0

        return BucketPnL(
            bucket=bucket_num,
            realized_pnl=round(realized, 2),
            unrealized_pnl=0.0,  # requires live marks; supplied by the dashboard
            premium_collected=round(float(premium_row["p"]), 2),
            win_rate=round(win_rate, 1),
            open_positions=int(open_count),
            mtd_return=round(mtd, 2),
        )

    def get_bucket_summary(self) -> dict[int, BucketPnL]:
        """All three buckets' P&L, keyed by bucket number (for the dashboard)."""
        return {b: self.get_bucket_pnl(b) for b in (1, 2, 3)}

    def record_classification(self, position_id: int, cls: BucketClassification) -> None:
        """Persist a classification back onto the positions row."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE positions SET bucket=?, sub_type=?, recoverable=? WHERE id=?",
                (cls.bucket, cls.sub_type, int(cls.recoverable), position_id),
            )
        logger.info(
            "BucketManager: position %d classified B%d/%s (recoverable=%s)",
            position_id, cls.bucket, cls.sub_type, cls.recoverable,
        )

    @staticmethod
    def _today() -> str:
        return datetime.now(tz=UTC).date().isoformat()


__all__ = ["BucketClassification", "BucketPnL", "BucketManager"]
