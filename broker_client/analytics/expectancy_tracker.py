"""
broker_client/analytics/expectancy_tracker.py

Per-strategy expectancy tracking:
  Expectancy = Win% × Avg_Win − Loss% × Avg_Loss

Reads closed trades from the DB and computes rolling expectancy
per strategy tag (bucket1_msp, bucket1_wheel, bucket2_iv_crush,
bucket2_iv_spike, bucket3_leap, bucket3_event_bounce).

Usage:
    from broker_client.analytics.expectancy_tracker import ExpectancyTracker
    tracker = ExpectancyTracker(db_path="trading.db")
    report = tracker.report()          # dict keyed by strategy
    tracker.save_to_db()              # persist to expectancy_log table
"""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ── schema ────────────────────────────────────────────────────────────────────
CREATE_EXPECTANCY_LOG = """
CREATE TABLE IF NOT EXISTS expectancy_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    window_days   INTEGER NOT NULL DEFAULT 90,
    n_trades      INTEGER NOT NULL DEFAULT 0,
    n_wins        INTEGER NOT NULL DEFAULT 0,
    n_losses      INTEGER NOT NULL DEFAULT 0,
    win_rate      REAL    NOT NULL DEFAULT 0,
    avg_win_pct   REAL    NOT NULL DEFAULT 0,
    avg_loss_pct  REAL    NOT NULL DEFAULT 0,
    expectancy    REAL    NOT NULL DEFAULT 0,
    profit_factor REAL,
    computed_at   TEXT    NOT NULL
);
"""

CREATE_TRADES_STRATEGY_COL = """
-- Run once if column missing:
-- ALTER TABLE trades ADD COLUMN strategy TEXT;
"""

# ── data class ────────────────────────────────────────────────────────────────
@dataclass
class StrategyExpectancy:
    strategy: str
    window_days: int
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    win_rate: float = 0.0        # 0–1
    avg_win_pct: float = 0.0     # avg return on winning trades (%)
    avg_loss_pct: float = 0.0    # avg return on losing trades (%) — positive number
    expectancy: float = 0.0      # win_rate * avg_win - loss_rate * avg_loss
    profit_factor: Optional[float] = None  # gross_profit / gross_loss
    computed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def loss_rate(self) -> float:
        return 1.0 - self.win_rate

    @property
    def grade(self) -> str:
        """Human-readable grade for dashboard."""
        if self.n_trades < 5:
            return "⚠️  Too few trades"
        if self.expectancy >= 1.5:
            return "🟢 Excellent"
        if self.expectancy >= 0.5:
            return "🟡 Good"
        if self.expectancy >= 0.0:
            return "🟠 Marginal"
        return "🔴 Negative"


# ── main class ────────────────────────────────────────────────────────────────
class ExpectancyTracker:
    """
    Computes per-strategy expectancy from the `trades` table.

    The `trades` table must have these columns:
        symbol, strategy, entry_price, exit_price, quantity,
        pnl_pct (optional; computed from entry/exit if missing),
        status  ('closed' | 'open')

    If your trades table uses different column names pass a column_map:
        ExpectancyTracker(db_path=..., column_map={
            "pnl_pct": "realized_pct",
            "status": "trade_status",
        })
    """

    KNOWN_STRATEGIES = [
        "bucket1_msp",
        "bucket1_wheel",
        "bucket2_iv_crush",
        "bucket2_iv_spike",
        "bucket3_leap",
        "bucket3_event_bounce",
        "untagged",
    ]

    def __init__(
        self,
        db_path: str = "trading.db",
        window_days: int = 90,
        column_map: Optional[dict] = None,
    ):
        self.db_path = db_path
        self.window_days = window_days
        self._col = {
            "pnl_pct": "pnl_pct",
            "status": "status",
            "strategy": "strategy",
            "closed_at": "closed_at",
            **(column_map or {}),
        }
        self._ensure_schema()

    # ── public api ─────────────────────────────────────────────────────────
    def report(self) -> dict[str, StrategyExpectancy]:
        """Return expectancy for every strategy seen in the window."""
        rows = self._fetch_closed_trades()
        by_strategy: dict[str, list[float]] = {}

        for row in rows:
            strat = row.get(self._col["strategy"]) or "untagged"
            pnl = self._get_pnl_pct(row)
            if pnl is None:
                continue
            by_strategy.setdefault(strat, []).append(pnl)

        result: dict[str, StrategyExpectancy] = {}
        for strat, pnls in by_strategy.items():
            result[strat] = self._compute(strat, pnls)

        # also add zero-trade entries for known strategies not yet traded
        for s in self.KNOWN_STRATEGIES:
            if s not in result:
                result[s] = StrategyExpectancy(
                    strategy=s,
                    window_days=self.window_days,
                )
        return result

    def save_to_db(self) -> None:
        """Persist current expectancy snapshot to expectancy_log table."""
        report = self.report()
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(CREATE_EXPECTANCY_LOG)
            for exp in report.values():
                conn.execute(
                    """INSERT INTO expectancy_log
                       (strategy, window_days, n_trades, n_wins, n_losses,
                        win_rate, avg_win_pct, avg_loss_pct, expectancy,
                        profit_factor, computed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        exp.strategy,
                        exp.window_days,
                        exp.n_trades,
                        exp.n_wins,
                        exp.n_losses,
                        exp.win_rate,
                        exp.avg_win_pct,
                        exp.avg_loss_pct,
                        exp.expectancy,
                        exp.profit_factor,
                        now,
                    ),
                )
        log.info("Expectancy snapshot saved for %d strategies", len(report))

    def latest_from_db(self) -> dict[str, StrategyExpectancy]:
        """Read most-recent snapshot per strategy from expectancy_log."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """SELECT * FROM expectancy_log e1
                       WHERE computed_at = (
                           SELECT MAX(computed_at) FROM expectancy_log e2
                           WHERE e2.strategy = e1.strategy
                       )"""
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        result = {}
        for r in rows:
            result[r["strategy"]] = StrategyExpectancy(**dict(r))
        return result

    # ── internals ──────────────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(CREATE_EXPECTANCY_LOG)
            # Add strategy column to trades if missing
            try:
                cols = [
                    c[1]
                    for c in conn.execute("PRAGMA table_info(trades)").fetchall()
                ]
                if "strategy" not in cols:
                    conn.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
                    log.info("Added 'strategy' column to trades table")
            except sqlite3.OperationalError:
                pass  # trades table may not exist yet

    def _fetch_closed_trades(self) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=self.window_days)).isoformat()
        status_col = self._col["status"]
        closed_col = self._col["closed_at"]

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    f"""SELECT * FROM trades
                        WHERE {status_col} = 'closed'
                        AND   {closed_col} >= ?""",
                    (cutoff,),
                ).fetchall()
            except sqlite3.OperationalError as e:
                log.warning("Could not query trades: %s", e)
                return []
        return [dict(r) for r in rows]

    def _get_pnl_pct(self, row: dict) -> Optional[float]:
        """Return P&L as percent (positive=profit, negative=loss)."""
        pnl_col = self._col["pnl_pct"]
        if pnl_col in row and row[pnl_col] is not None:
            return float(row[pnl_col])
        # Compute from entry/exit if column missing
        try:
            entry = float(row.get("entry_price", 0) or 0)
            exit_ = float(row.get("exit_price", 0) or 0)
            if entry > 0:
                return (exit_ - entry) / entry * 100
        except (TypeError, ValueError):
            pass
        return None

    def _compute(self, strategy: str, pnls: list[float]) -> StrategyExpectancy:
        if not pnls:
            return StrategyExpectancy(strategy=strategy, window_days=self.window_days)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        n = len(pnls)
        win_rate = len(wins) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        return StrategyExpectancy(
            strategy=strategy,
            window_days=self.window_days,
            n_trades=n,
            n_wins=len(wins),
            n_losses=len(losses),
            win_rate=win_rate,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            expectancy=expectancy,
            profit_factor=profit_factor,
        )


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json, pathlib

    db = sys.argv[1] if len(sys.argv) > 1 else "trading.db"
    tracker = ExpectancyTracker(db_path=db)
    report = tracker.report()

    print(f"\n{'Strategy':<25} {'N':>5} {'WinRate':>8} {'AvgWin':>8} {'AvgLoss':>8} {'Expect':>8}  Grade")
    print("-" * 80)
    for strat, e in sorted(report.items()):
        pf = f"{e.profit_factor:.2f}" if e.profit_factor else "  n/a"
        print(
            f"{strat:<25} {e.n_trades:>5} {e.win_rate:>7.1%} "
            f"{e.avg_win_pct:>7.2f}% {e.avg_loss_pct:>7.2f}%  "
            f"{e.expectancy:>7.2f}%  {e.grade}"
        )

    tracker.save_to_db()
    print("\n✅ Snapshot saved to expectancy_log table")
