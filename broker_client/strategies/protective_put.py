"""Protective put hedge strategy (Day 10)."""

from __future__ import annotations

from config.settings import settings
from broker_core.base_broker import Account, OptionsOrder, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal


class ProtectivePut(BaseStrategy):
    """Buy OTM puts to hedge long equity on bearish regime signal."""

    strategy_name = "protective_put"
    strategy_type = "options hedge"
    timeframe = "short"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        return (
            signal.direction in ("short", "strong_short")
            and signal.source == "fusion"
            and signal.confidence >= settings.CONFIDENCE_HIGH
        )

    def build_order(self, signal: Signal, account: Account) -> OptionsOrder:
        ticker = signal.metadata.get("ticker", "SPY")
        return OptionsOrder(
            ticker=ticker,
            action="BUY_TO_OPEN",
            contract=f"{ticker}000000P00000000",
            qty=1,
            order_type="limit",
            limit_price=signal.metadata.get("put_mid", 2.0),
            strategy_name=self.strategy_name,
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        return False  # exits when regime returns to neutral (position_watcher)

    def build_exit_order(self, position: Position) -> OptionsOrder:
        return OptionsOrder(
            ticker=position.ticker,
            action="SELL_TO_CLOSE",
            contract=f"{position.ticker}000000P00000000",
            qty=abs(position.qty),
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def describe(self) -> str:
        return "ProtectivePut: buys OTM puts to hedge longs on bearish regime signal."


__all__ = ["ProtectivePut"]
