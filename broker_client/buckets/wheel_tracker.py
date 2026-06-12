"""Wheel-strategy cycle tracker (Phase 2 Step 8).

Models the wheel as a state machine per ticker:

    sell_put → assigned → sell_call → called_away → (restart)

Each cycle is one row in the ``wheel_cycles`` table; the queryable columns
(phase, strike, premium_collected, cost_basis, total_cycle_return) are kept in
sync, while the full per-leg detail (put/call strikes, premiums, dates) lives in
a JSON blob in ``notes`` so a rich ``WheelCycle`` view can be reconstructed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_MULT = settings.OPTIONS_CONTRACT_MULTIPLIER

# Terminal phases — a cycle in one of these is "closed" and starts a new one.
_TERMINAL = {"called_away", "restart"}


class WheelCycle(BaseModel):
    """A full wheel cycle for one ticker (reconstructed from a row + detail)."""

    ticker: str
    cycle_number: int
    current_phase: str

    put_strike: float | None = None
    put_premium: float | None = None
    put_expiry: str | None = None
    put_filled_date: str | None = None

    assigned: bool = False
    assignment_date: str | None = None
    cost_basis: float | None = None
    shares: int = _MULT

    call_strike: float | None = None
    call_premium: float | None = None
    call_expiry: str | None = None
    call_filled_date: str | None = None

    total_premium_collected: float = 0.0
    cycle_start_date: str | None = None
    cycle_end_date: str | None = None
    cycle_return_pct: float | None = None
    annualized_return_pct: float | None = None


class WheelSummary(BaseModel):
    """Aggregated wheel performance for one ticker."""

    ticker: str
    total_cycles: int
    current_phase: str
    total_premium_collected: float
    avg_cycle_return_pct: float
    avg_annualized_return_pct: float
    best_cycle: WheelCycle | None = None
    worst_cycle: WheelCycle | None = None


class WheelTracker:
    """Records and summarises wheel cycles in the ``wheel_cycles`` table."""

    def __init__(self, db_path: str | None = None, alert_engine: Any = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._alerts = alert_engine

    # ── recording ────────────────────────────────────────────────────────────
    def record_put_sale(
        self, ticker: str, strike: float, premium: float, expiry: str,
        shares: int = _MULT,
    ) -> int:
        """Open a new wheel cycle in the sell_put phase. Returns the row id."""
        ticker = ticker.upper()
        cycle_number = self._next_cycle_number(ticker)
        # premium passed per share → dollars over the shares covered.
        premium_dollars = premium * shares
        today = self._today()
        detail = {
            "put_strike": strike,
            "put_premium": premium,
            "put_expiry": expiry,
            "put_filled_date": today,
            "put_premium_dollars": premium_dollars,
            "shares": shares,
            "cycle_start_date": today,
            "assigned": False,
        }
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO wheel_cycles
                   (ticker, phase, phase_entry_date, strike, shares,
                    premium_collected, cycle_number, notes)
                   VALUES (?, 'sell_put', ?, ?, ?, ?, ?, ?)""",
                (ticker, today, strike, shares, premium_dollars, cycle_number,
                 json.dumps(detail)),
            )
            row_id = int(cur.lastrowid or 0)
        logger.info(
            "WheelTracker: %s cycle #%d put sold @ %.2f (premium $%.2f)",
            ticker, cycle_number, strike, premium_dollars,
        )
        return row_id

    def record_assignment(
        self, ticker: str, shares: int, cost_basis: float,
    ) -> None:
        """Mark the open cycle assigned (we now own the shares)."""
        row = self._latest_open_cycle(ticker)
        if row is None:
            logger.warning("WheelTracker: no open cycle for %s assignment", ticker)
            return
        detail = self._detail(row)
        today = self._today()
        detail.update(assigned=True, assignment_date=today,
                      cost_basis=cost_basis, shares=shares)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE wheel_cycles SET phase='assigned', shares=?, cost_basis=?, "
                "notes=? WHERE id=?",
                (shares, cost_basis, json.dumps(detail), row["id"]),
            )
        self._alert(
            f"📥 ASSIGNED: {ticker}",
            f"{ticker} assigned at ${detail.get('put_strike')} — now own {shares} shares "
            f"(cost basis ${cost_basis:.2f})",
        )
        logger.info("WheelTracker: %s assigned %d shares @ basis %.2f", ticker, shares, cost_basis)

    def record_call_sale(
        self, ticker: str, strike: float, premium: float, expiry: str,
    ) -> None:
        """Record the covered-call leg against assigned shares."""
        row = self._latest_open_cycle(ticker)
        if row is None:
            logger.warning("WheelTracker: no open cycle for %s call sale", ticker)
            return
        detail = self._detail(row)
        shares = int(detail.get("shares", row["shares"] or _MULT))
        call_premium_dollars = premium * shares
        today = self._today()
        detail.update(call_strike=strike, call_premium=premium, call_expiry=expiry,
                      call_filled_date=today, call_premium_dollars=call_premium_dollars)
        new_premium = (row["premium_collected"] or 0.0) + call_premium_dollars
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE wheel_cycles SET phase='sell_call', strike=?, "
                "premium_collected=?, notes=? WHERE id=?",
                (strike, new_premium, json.dumps(detail), row["id"]),
            )
        logger.info("WheelTracker: %s call sold @ %.2f (premium $%.2f)", ticker, strike, call_premium_dollars)

    def record_called_away(self, ticker: str, proceeds: float) -> WheelCycle | None:
        """Close the cycle — shares called away at ``proceeds`` per share."""
        row = self._latest_open_cycle(ticker)
        if row is None:
            logger.warning("WheelTracker: no open cycle for %s called-away", ticker)
            return None
        detail = self._detail(row)
        shares = int(detail.get("shares", row["shares"] or _MULT))
        cost_basis = float(detail.get("cost_basis") or row["cost_basis"] or 0.0)
        call_premium_dollars = float(detail.get("call_premium_dollars", 0.0))

        # Capital gain on the shares + the covered-call premium (put premium is
        # already embedded in the reduced cost basis, so it is not double-counted).
        capital_pnl = (proceeds - cost_basis) * shares
        total_return = capital_pnl + call_premium_dollars
        invested = cost_basis * shares
        return_pct = (total_return / invested * 100.0) if invested > 0 else 0.0

        today = self._today()
        start = detail.get("cycle_start_date")
        days = self._days_between(start, today)
        annualized = return_pct * (365.0 / max(days, 1))
        detail.update(cycle_end_date=today, proceeds=proceeds,
                      cycle_return_pct=round(return_pct, 2),
                      annualized_return_pct=round(annualized, 2))

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE wheel_cycles SET phase='called_away', phase_exit_date=?, "
                "total_cycle_return=?, notes=? WHERE id=?",
                (today, round(total_return, 2), json.dumps(detail), row["id"]),
            )
        self._alert(
            f"📤 CALLED AWAY: {ticker}",
            f"{ticker} called away at ${proceeds:.2f} — cycle return {return_pct:+.1f}% "
            f"(annualized {annualized:+.1f}%). Restart wheel?",
        )
        logger.info(
            "WheelTracker: %s called away @ %.2f → return %.1f%% (ann %.1f%%)",
            ticker, proceeds, return_pct, annualized,
        )
        return self._to_cycle(self._row_by_id(row["id"]))

    # ── summaries ────────────────────────────────────────────────────────────
    def get_wheel_summary(self, ticker: str) -> WheelSummary | None:
        ticker = ticker.upper()
        cycles = self.get_cycles(ticker)
        if not cycles:
            return None
        completed = [c for c in cycles if c.cycle_return_pct is not None]
        returns = [c.cycle_return_pct or 0.0 for c in completed]
        ann = [c.annualized_return_pct or 0.0 for c in completed]
        best = max(completed, key=lambda c: c.cycle_return_pct or 0.0, default=None)
        worst = min(completed, key=lambda c: c.cycle_return_pct or 0.0, default=None)
        return WheelSummary(
            ticker=ticker,
            total_cycles=len(cycles),
            current_phase=cycles[-1].current_phase,
            total_premium_collected=round(sum(c.total_premium_collected for c in cycles), 2),
            avg_cycle_return_pct=round(sum(returns) / len(returns), 2) if returns else 0.0,
            avg_annualized_return_pct=round(sum(ann) / len(ann), 2) if ann else 0.0,
            best_cycle=best,
            worst_cycle=worst,
        )

    def get_all_wheels(self) -> list[WheelSummary]:
        with sqlite3.connect(self._db_path) as conn:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM wheel_cycles ORDER BY ticker"
            )]
        out = []
        for t in tickers:
            summary = self.get_wheel_summary(t)
            if summary is not None:
                out.append(summary)
        return out

    def get_cycles(self, ticker: str) -> list[WheelCycle]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM wheel_cycles WHERE ticker=? ORDER BY cycle_number, id",
                (ticker.upper(),),
            ).fetchall()
        return [self._to_cycle(r) for r in rows]

    # ── helpers ──────────────────────────────────────────────────────────────
    def _next_cycle_number(self, ticker: str) -> int:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT MAX(cycle_number) FROM wheel_cycles WHERE ticker=?",
                (ticker,),
            ).fetchone()
        return int(row[0] or 0) + 1

    def _latest_open_cycle(self, ticker: str) -> sqlite3.Row | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM wheel_cycles WHERE ticker=? ORDER BY id DESC",
                (ticker.upper(),),
            ).fetchall()
        for r in rows:
            row: sqlite3.Row = r
            if row["phase"] not in _TERMINAL:
                return row
        return None

    def _row_by_id(self, row_id: int) -> sqlite3.Row:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row: sqlite3.Row = conn.execute(
                "SELECT * FROM wheel_cycles WHERE id=?", (row_id,)
            ).fetchone()
        return row

    @staticmethod
    def _detail(row: sqlite3.Row) -> dict:
        try:
            return json.loads(row["notes"]) if row["notes"] else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _to_cycle(self, row: sqlite3.Row) -> WheelCycle:
        d = self._detail(row)
        return WheelCycle(
            ticker=row["ticker"],
            cycle_number=row["cycle_number"] or 1,
            current_phase=row["phase"],
            put_strike=d.get("put_strike"),
            put_premium=d.get("put_premium"),
            put_expiry=d.get("put_expiry"),
            put_filled_date=d.get("put_filled_date"),
            assigned=bool(d.get("assigned", False)),
            assignment_date=d.get("assignment_date"),
            cost_basis=d.get("cost_basis", row["cost_basis"]),
            shares=int(d.get("shares", row["shares"] or _MULT)),
            call_strike=d.get("call_strike"),
            call_premium=d.get("call_premium"),
            call_expiry=d.get("call_expiry"),
            call_filled_date=d.get("call_filled_date"),
            total_premium_collected=row["premium_collected"] or 0.0,
            cycle_start_date=d.get("cycle_start_date"),
            cycle_end_date=d.get("cycle_end_date"),
            cycle_return_pct=d.get("cycle_return_pct"),
            annualized_return_pct=d.get("annualized_return_pct"),
        )

    def _alert(self, title: str, body: str) -> None:
        if self._alerts is None:
            return
        try:
            self._alerts.send_alert(title=title, body=body, channel="signals", color="yellow")
        except Exception as exc:
            logger.error("WheelTracker: alert failed: %s", exc)

    @staticmethod
    def _today() -> str:
        return datetime.now(tz=UTC).date().isoformat()

    @staticmethod
    def _days_between(start: str | None, end: str) -> int:
        if not start:
            return 1
        try:
            return max((date.fromisoformat(end) - date.fromisoformat(start)).days, 1)
        except (ValueError, TypeError):
            return 1


__all__ = ["WheelCycle", "WheelSummary", "WheelTracker"]
