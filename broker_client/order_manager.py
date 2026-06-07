"""Order manager — constructs, submits, and logs fills to SQLite (Day 8)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import Order, OptionsOrder, OrderResult

logger = get_logger(__name__)


class OrderManager:
    """Wraps order submission and persistence.

    After a fill, records to the trades SQLite table and optionally
    notifies PositionWatcher.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._position_watcher = None  # injected after construction

    def set_position_watcher(self, watcher) -> None:
        self._position_watcher = watcher

    def record(self, order: Order | OptionsOrder, result: OrderResult) -> None:
        """Persist a trade to SQLite and notify PositionWatcher on fill."""
        if result.status not in ("filled", "partial"):
            logger.debug("OrderManager.record: status=%s — not persisting", result.status)
            return

        is_options = isinstance(order, OptionsOrder)
        position_type = "options_long" if is_options else (
            "equity_long" if order.action in ("BUY",) else "equity_short"
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO trades
                   (account_id, ticker, strategy, position_type, action,
                    qty, fill_price, commission, order_id, env, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.account_id,
                    order.ticker,
                    order.strategy_name,
                    position_type,
                    order.action,
                    order.qty,
                    result.fill_price,
                    result.commission,
                    result.order_id,
                    settings.ENV,
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )

        logger.info(
            "OrderManager: recorded fill %s %s x%d @ %.4f (commission=%.4f)",
            order.action, order.ticker, order.qty, result.fill_price, result.commission,
        )

        if self._position_watcher is not None:
            try:
                self._position_watcher.register_if_filled(order, result)
            except Exception as exc:
                logger.error("OrderManager: position watcher notify failed: %s", exc)


__all__ = ["OrderManager"]
