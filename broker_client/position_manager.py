"""Position manager — open/close tracking, P&L, stop/take-profit (Day 9)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class PositionManager:
    """Tracks open positions in SQLite and maintains in-memory cache."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._cache: dict[str, dict] = {}  # ticker → position row dict

    def open_position(
        self,
        account_id: str,
        ticker: str,
        strategy: str,
        position_type: str,
        qty: int,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> int:
        """Insert a new open position. Returns the row id."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO positions
                   (account_id, ticker, strategy, position_type, qty, entry_price,
                    stop_loss, take_profit, opened_at, is_open)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    account_id, ticker, strategy, position_type, qty, entry_price,
                    stop_loss, take_profit,
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
            row_id = cur.lastrowid or 0
        self._cache[ticker] = {
            "id": row_id, "account_id": account_id, "ticker": ticker,
            "strategy": strategy, "position_type": position_type,
            "qty": qty, "entry_price": entry_price,
            "stop_loss": stop_loss, "take_profit": take_profit,
        }
        logger.info(
            "PositionManager: opened %s x%d @ %.4f stop=%.4f tp=%.4f",
            ticker, qty, entry_price, stop_loss or 0, take_profit or 0,
        )
        return row_id

    def close_position(
        self,
        ticker: str,
        exit_price: float,
        exit_reason: str = "manual",
    ) -> Optional[float]:
        """Close an open position and return realized P&L, or None if not found."""
        pos = self._cache.get(ticker)
        if pos is None:
            # Try DB
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id, qty, entry_price FROM positions WHERE ticker=? AND is_open=1",
                    (ticker,),
                ).fetchone()
            if row is None:
                logger.warning("PositionManager: no open position for %s", ticker)
                return None
            pos = {"id": row[0], "qty": row[1], "entry_price": row[2]}

        realized_pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        closed_at = datetime.now(tz=timezone.utc).isoformat()

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE positions
                   SET is_open=0, closed_at=?, exit_reason=?, realized_pnl=?
                   WHERE id=? AND is_open=1""",
                (closed_at, exit_reason, realized_pnl, pos["id"]),
            )

        self._cache.pop(ticker, None)
        logger.info(
            "PositionManager: closed %s pnl=%.4f reason=%s",
            ticker, realized_pnl, exit_reason,
        )
        return float(realized_pnl)

    def get_open_positions(self, account_id: Optional[str] = None) -> list[dict]:
        """Return all open positions (from DB)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            if account_id:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE is_open=1 AND account_id=?",
                    (account_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE is_open=1"
                ).fetchall()
        return [dict(r) for r in rows]

    def update_stop_loss(self, ticker: str, new_stop: float) -> None:
        """Update the stop-loss for an open position (trailing stop)."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE positions SET stop_loss=? WHERE ticker=? AND is_open=1",
                (new_stop, ticker),
            )
        if ticker in self._cache:
            self._cache[ticker]["stop_loss"] = new_stop

    def update_take_profit(self, ticker: str, new_target: float) -> None:
        """Update the take-profit target for an open position."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE positions SET take_profit=? WHERE ticker=? AND is_open=1",
                (new_target, ticker),
            )
        if ticker in self._cache:
            self._cache[ticker]["take_profit"] = new_target

    def update_levels(self, ticker: str, stop_loss: float | None, take_profit: float | None) -> None:
        """Update stop-loss and/or take-profit in one call."""
        if stop_loss is not None:
            self.update_stop_loss(ticker, stop_loss)
        if take_profit is not None:
            self.update_take_profit(ticker, take_profit)


__all__ = ["PositionManager"]
