"""BaseStrategy ABC (Day 8, CLAUDE.md Section 9)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from broker_core.base_broker import Account, Order, OptionsOrder, Position
from signals.signal_schema import MarketSnapshot, Signal


class BaseStrategy(ABC):
    """All strategies inherit from this class.

    TradingEngine calls should_enter → build_order → (after fill) registers
    position. PositionWatcher calls should_exit → build_exit_order.
    """

    strategy_name: str = "base"
    strategy_type: str = "equity"   # equity | options | spread
    timeframe: str = "short"        # short | mid | long

    @abstractmethod
    def should_enter(self, signal: Signal, account: Account) -> bool:
        """Return True if this strategy wants to open a position now."""
        ...

    @abstractmethod
    def build_order(self, signal: Signal, account: Account) -> Order | OptionsOrder:
        """Build the entry order given the signal and current account state."""
        ...

    @abstractmethod
    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        """Return True if this open position should be closed now."""
        ...

    @abstractmethod
    def build_exit_order(self, position: Position) -> Order | OptionsOrder:
        """Build the exit order for an open position."""
        ...

    @abstractmethod
    def describe(self) -> str:
        """Human-readable description for logs and dashboard."""
        ...


__all__ = ["BaseStrategy"]
