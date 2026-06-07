"""PaperAccount — thread-safe JSON state for a single paper account (Day 7).

Persists to paper_trading/accounts/{account_id}.json using filelock.
Supports multiple named accounts simultaneously.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import Order, OrderPreview, OrderResult

logger = get_logger(__name__)


class OpenPosition(BaseModel):
    ticker: str
    strategy: str
    position_type: str = "equity_long"
    qty: int
    entry_price: float
    entry_date: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: float = 0.0
    unrealized_pnl: float = 0.0


class BalanceEntry(BaseModel):
    timestamp: str
    cash: float
    action: str
    note: str = ""


class DailyEquityEntry(BaseModel):
    date: str
    equity: float


class AccountPerformance(BaseModel):
    account_id: str
    initial_cash: float
    current_cash: float
    current_equity: float
    total_return_pct: float
    open_positions_count: int
    closed_positions_count: int


class AccountState(BaseModel):
    account_id: str
    created_at: str
    initial_cash: float = 0.0
    cash: float = 0.0
    equity: float = 0.0
    open_positions: list[OpenPosition] = Field(default_factory=list)
    closed_positions: list[dict] = Field(default_factory=list)
    balance_history: list[BalanceEntry] = Field(default_factory=list)
    daily_equity_history: list[DailyEquityEntry] = Field(default_factory=list)


class PaperAccount:
    """Thread-safe paper trading account backed by a JSON file."""

    def __init__(self, account_id: str, accounts_dir: Optional[str] = None) -> None:
        self.account_id = account_id
        self._dir = Path(accounts_dir or settings.PAPER_ACCOUNTS_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / f"{account_id}.json"
        self._lock_file = self._dir / f"{account_id}.lock"
        self._lock = FileLock(str(self._lock_file), timeout=10)

        if not self._file.exists():
            self._write(AccountState(
                account_id=account_id,
                created_at=datetime.now(tz=timezone.utc).isoformat(),
            ))

    def _read(self) -> AccountState:
        with self._lock:
            with open(self._file) as f:
                return AccountState.model_validate(json.load(f))

    def _write(self, state: AccountState) -> None:
        with self._lock:
            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)

    def initialize_balance(self, cash: float, note: str = "Starting balance") -> None:
        """Set the account's starting cash balance."""
        with self._lock:
            with open(self._file) as f:
                state = AccountState.model_validate(json.load(f))
            state.initial_cash = cash
            state.cash = cash
            state.equity = cash
            state.balance_history.append(BalanceEntry(
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                cash=cash,
                action="init",
                note=note,
            ))
            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)
        logger.info("PaperAccount[%s]: balance initialized to $%.2f", self.account_id, cash)

    def manual_adjustment(self, amount: float, reason: str = "") -> None:
        """Add or remove cash with a logged reason."""
        with self._lock:
            with open(self._file) as f:
                state = AccountState.model_validate(json.load(f))
            state.cash += amount
            state.equity += amount
            state.balance_history.append(BalanceEntry(
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                cash=state.cash,
                action="adjustment",
                note=reason or f"Manual: {amount:+.2f}",
            ))
            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)
        logger.info("PaperAccount[%s]: manual adjustment %.2f (%s)", self.account_id, amount, reason)

    def apply_fill(self, order: Order, fill_price: float, commission: float = 0.0) -> None:
        """Update positions and cash after a simulated fill."""
        with self._lock:
            with open(self._file) as f:
                state = AccountState.model_validate(json.load(f))

            cost = fill_price * order.qty + commission

            if order.action in ("BUY", "BUY_TO_OPEN"):
                state.cash -= cost
                pos = OpenPosition(
                    ticker=order.ticker,
                    strategy=order.strategy_name,
                    qty=order.qty,
                    entry_price=fill_price,
                    entry_date=datetime.now(tz=timezone.utc).date().isoformat(),
                )
                state.open_positions.append(pos)
                logger.info("PaperAccount[%s]: BUY %s x%d @ %.4f", self.account_id, order.ticker, order.qty, fill_price)

            elif order.action in ("SELL", "SELL_TO_CLOSE"):
                # Find matching long position
                remaining = order.qty
                for pos in list(state.open_positions):
                    if pos.ticker == order.ticker and remaining > 0:
                        closed_qty = min(pos.qty, remaining)
                        realized_pnl = (fill_price - pos.entry_price) * closed_qty - commission
                        state.cash += fill_price * closed_qty - commission
                        closed = {
                            "ticker": pos.ticker,
                            "strategy": pos.strategy,
                            "qty": closed_qty,
                            "entry_price": pos.entry_price,
                            "exit_price": fill_price,
                            "realized_pnl": realized_pnl,
                            "closed_at": datetime.now(tz=timezone.utc).isoformat(),
                        }
                        state.closed_positions.append(closed)
                        pos.qty -= closed_qty
                        remaining -= closed_qty
                        if pos.qty <= 0:
                            state.open_positions.remove(pos)
                logger.info("PaperAccount[%s]: SELL %s x%d @ %.4f", self.account_id, order.ticker, order.qty, fill_price)

            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)

    def apply_preview(self, order: Order, preview: OrderPreview) -> OrderResult:
        """Apply a Schwab previewOrder result to the paper account."""
        if not preview.is_valid:
            return OrderResult(
                order_id="PREVIEW_REJECTED",
                status="rejected",
                fill_price=0.0,
            )
        fill_price = preview.estimated_cost / max(order.qty, 1)
        self.apply_fill(order, fill_price, preview.fees)
        return OrderResult(
            order_id=f"PAPER-{order.ticker}-{order.qty}",
            status="filled",
            fill_price=fill_price,
            commission=preview.fees,
        )

    def reset(self, confirm: bool = True, new_balance: float = 0.0) -> None:
        """Wipe positions, keep trade history, restart from new_balance."""
        if not confirm:
            return
        with self._lock:
            with open(self._file) as f:
                state = AccountState.model_validate(json.load(f))
            closed = state.closed_positions
            state = AccountState(
                account_id=self.account_id,
                created_at=datetime.now(tz=timezone.utc).isoformat(),
                initial_cash=new_balance,
                cash=new_balance,
                equity=new_balance,
                closed_positions=closed,
            )
            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)
        logger.warning("PaperAccount[%s]: RESET (new_balance=%.2f)", self.account_id, new_balance)

    def get_state(self) -> AccountState:
        return self._read()

    def get_performance(self) -> AccountPerformance:
        state = self._read()
        total_return_pct = (
            (state.equity - state.initial_cash) / state.initial_cash * 100
            if state.initial_cash > 0 else 0.0
        )
        return AccountPerformance(
            account_id=self.account_id,
            initial_cash=state.initial_cash,
            current_cash=state.cash,
            current_equity=state.equity,
            total_return_pct=total_return_pct,
            open_positions_count=len(state.open_positions),
            closed_positions_count=len(state.closed_positions),
        )

    def record_daily_equity(self) -> None:
        """EOD snapshot — call once at 4:05 PM."""
        with self._lock:
            with open(self._file) as f:
                state = AccountState.model_validate(json.load(f))
            state.daily_equity_history.append(DailyEquityEntry(
                date=datetime.now(tz=timezone.utc).date().isoformat(),
                equity=state.equity,
            ))
            with open(self._file, "w") as f:
                json.dump(state.model_dump(), f, indent=2, default=str)


__all__ = ["PaperAccount", "AccountState", "AccountPerformance"]
