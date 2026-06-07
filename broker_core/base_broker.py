"""BaseBroker ABC + all pydantic models (Day 6, CLAUDE.md Section 7)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Order types ──────────────────────────────────────────────────────────────

OrderType = Literal["market", "limit", "stop", "stop_limit"]
OrderAction = Literal["BUY", "SELL", "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"]
OrderStatus = Literal["pending", "filled", "partial", "cancelled", "rejected", "dry_run"]


class Order(BaseModel):
    """Equity order submitted to the broker."""

    ticker: str
    action: OrderAction
    qty: int = Field(gt=0)
    order_type: OrderType = "market"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    strategy_name: str = "unknown"
    account_id: str = "default"
    time_in_force: str = "day"  # day | gtc | ioc | fok


class OptionsOrder(BaseModel):
    """Options order submitted to the broker."""

    ticker: str
    action: OrderAction
    contract: str          # OCC symbol e.g. "AAPL240119C00150000"
    qty: int = Field(gt=0)  # number of contracts
    order_type: OrderType = "limit"
    limit_price: Optional[float] = None
    strategy_name: str = "unknown"
    account_id: str = "default"
    time_in_force: str = "day"


class OrderResult(BaseModel):
    """Result of a placed order."""

    order_id: str
    fill_price: float = 0.0
    commission: float = 0.0
    filled_at: Optional[datetime] = None
    status: OrderStatus = "pending"
    raw_response: Optional[dict] = None


class OrderPreview(BaseModel):
    """Dry-run preview of an order (no real submission)."""

    estimated_cost: float = 0.0
    buying_power_effect: float = 0.0
    margin_impact: float = 0.0
    fees: float = 0.0
    is_valid: bool = True
    rejection_reason: Optional[str] = None


class Position(BaseModel):
    """Open position (equity or options)."""

    ticker: str
    qty: int
    avg_cost: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    strategy_name: str = "unknown"
    position_type: str = "equity_long"  # equity_long | equity_short | options_long | …
    opened_at: Optional[datetime] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class Account(BaseModel):
    """Broker account summary."""

    account_id: str
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    margin_used: float = 0.0


class Quote(BaseModel):
    """Real-time quote for a ticker."""

    ticker: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    timestamp: Optional[datetime] = None


class OptionsContract(BaseModel):
    """Single options contract from a chain."""

    symbol: str        # OCC symbol
    expiry: str        # YYYY-MM-DD
    strike: float
    option_type: Literal["call", "put"]
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    implied_vol: Optional[float] = None


class OptionsChain(BaseModel):
    """Full options chain for one underlying."""

    ticker: str
    expiry: str
    calls: list[OptionsContract] = Field(default_factory=list)
    puts: list[OptionsContract] = Field(default_factory=list)


class MarketHours(BaseModel):
    """Market session hours."""

    is_open: bool
    market_open: Optional[datetime] = None
    market_close: Optional[datetime] = None
    session: str = "regular"  # pre | regular | post | closed


class Transaction(BaseModel):
    """Historical transaction record."""

    transaction_id: str
    ticker: str
    action: str
    qty: int
    price: float
    commission: float = 0.0
    executed_at: Optional[datetime] = None
    description: str = ""


# ── BaseBroker ABC ────────────────────────────────────────────────────────────

class BaseBroker(ABC):
    """Abstract base broker — every broker implementation must implement ALL methods."""

    # Account
    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_transactions(self, days: int = 30) -> list[Transaction]: ...

    # Orders — equity
    @abstractmethod
    def place_order(self, order: Order) -> OrderResult: ...

    @abstractmethod
    def preview_order(self, order: Order) -> OrderPreview: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    def get_order_history(self, days: int = 30) -> list[Order]: ...

    # Orders — options
    @abstractmethod
    def place_options_order(self, order: OptionsOrder) -> OrderResult: ...

    @abstractmethod
    def preview_options_order(self, order: OptionsOrder) -> OrderPreview: ...

    # Market data
    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> OptionsChain: ...

    @abstractmethod
    def get_market_hours(self) -> MarketHours: ...

    @abstractmethod
    def stream_quotes(self, tickers: list[str], callback: Any) -> None: ...


__all__ = [
    "Order",
    "OptionsOrder",
    "OrderResult",
    "OrderPreview",
    "OrderStatus",
    "Position",
    "Account",
    "Quote",
    "OptionsContract",
    "OptionsChain",
    "MarketHours",
    "Transaction",
    "BaseBroker",
]
