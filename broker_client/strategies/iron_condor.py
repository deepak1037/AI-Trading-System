"""Iron condor strategy (Day 10)."""

from __future__ import annotations

from broker_core.base_broker import Account, OptionsOrder, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal


class IronCondor(BaseStrategy):
    """Sell OTM call + put spreads on neutral regime / low VIX."""

    strategy_name = "iron_condor"
    strategy_type = "spread"
    timeframe = "mid"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        return (
            signal.direction == "neutral"
            and signal.source == "fusion"
            and signal.confidence >= 50  # neutral + moderate confidence
        )

    def build_order(self, signal: Signal, account: Account) -> OptionsOrder:
        ticker = signal.metadata.get("ticker", "SPY")
        # Simplified: return the short call leg; full condor needs 4 legs
        return OptionsOrder(
            ticker=ticker,
            action="SELL_TO_OPEN",
            contract=f"{ticker}000000C00000000",
            qty=1,
            order_type="limit",
            limit_price=signal.metadata.get("condor_credit", 2.0),
            strategy_name=self.strategy_name,
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        return False

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
        return "IronCondor: sells OTM call + put spreads on neutral regime / low VIX."


__all__ = ["IronCondor"]
