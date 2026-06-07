"""Covered call strategy (Day 10)."""

from __future__ import annotations

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import Account, OptionsOrder, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal

logger = get_logger(__name__)


class CoveredCall(BaseStrategy):
    """Sell OTM calls against an existing long equity position on neutral signal."""

    strategy_name = "covered_call"
    strategy_type = "options"
    timeframe = "mid"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        return (
            signal.direction in ("neutral", "long")
            and signal.source in ("fusion", "technical")
            and signal.confidence >= settings.CONFIDENCE_HIGH
        )

    def build_order(self, signal: Signal, account: Account) -> OptionsOrder:
        ticker = signal.metadata.get("ticker", "UNKNOWN")
        return OptionsOrder(
            ticker=ticker,
            action="SELL_TO_OPEN",
            contract=f"{ticker}000000C00000000",  # placeholder OCC — real strike from OptionsEngine
            qty=1,
            order_type="limit",
            limit_price=signal.metadata.get("call_mid", 1.0),
            strategy_name=self.strategy_name,
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        return False  # managed by expiry / roll logic

    def build_exit_order(self, position: Position) -> OptionsOrder:
        return OptionsOrder(
            ticker=position.ticker,
            action="BUY_TO_CLOSE",
            contract=f"{position.ticker}000000C00000000",
            qty=abs(position.qty),
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def describe(self) -> str:
        return "CoveredCall: sells OTM calls against long equity on neutral signal."


__all__ = ["CoveredCall"]
