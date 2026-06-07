"""Cash-secured put strategy (Day 10)."""

from __future__ import annotations

from config.settings import settings
from broker_core.base_broker import Account, OptionsOrder, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal


class CashSecuredPut(BaseStrategy):
    """Sell OTM puts on watchlist stocks for premium or acquisition."""

    strategy_name = "cash_secured_put"
    strategy_type = "options"
    timeframe = "short"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        return (
            signal.direction in ("neutral", "long")
            and signal.source in ("fusion", "technical")
            and signal.confidence >= settings.CONFIDENCE_HIGH
            and account.cash >= account.equity * settings.MAX_POSITION_PCT
        )

    def build_order(self, signal: Signal, account: Account) -> OptionsOrder:
        ticker = signal.metadata.get("ticker", "UNKNOWN")
        return OptionsOrder(
            ticker=ticker,
            action="SELL_TO_OPEN",
            contract=f"{ticker}000000P00000000",
            qty=1,
            order_type="limit",
            limit_price=signal.metadata.get("put_mid", 1.0),
            strategy_name=self.strategy_name,
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        return False

    def build_exit_order(self, position: Position) -> OptionsOrder:
        return OptionsOrder(
            ticker=position.ticker,
            action="BUY_TO_CLOSE",
            contract=f"{position.ticker}000000P00000000",
            qty=abs(position.qty),
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def describe(self) -> str:
        return "CashSecuredPut: sells OTM puts on watchlist stocks for premium."


__all__ = ["CashSecuredPut"]
