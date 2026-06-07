"""Momentum breakout strategy (Day 8).

Enters on Stage 2 breakout from scanner watchlist. Exits on trail stop
or base breakdown.
"""

from __future__ import annotations

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import Account, Order, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal

logger = get_logger(__name__)

_BREAKOUT_DIRECTIONS = ("long", "strong_long")


class MomentumBreakout(BaseStrategy):
    """Buys breakouts from scanner watchlist on strong bullish signals."""

    strategy_name = "momentum_breakout"
    strategy_type = "equity"
    timeframe = "mid"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        if signal.source != "technical":
            return False
        if signal.direction not in _BREAKOUT_DIRECTIONS:
            return False
        if signal.confidence < settings.CONFIDENCE_HIGH:
            return False
        ticker = signal.metadata.get("ticker", "")
        if not ticker:
            return False
        return account.buying_power > account.equity * settings.MAX_POSITION_PCT

    def build_order(self, signal: Signal, account: Account) -> Order:
        position_value = account.equity * settings.MAX_POSITION_PCT
        ticker = signal.metadata.get("ticker", "UNKNOWN")
        return Order(
            ticker=ticker,
            action="BUY",
            qty=max(1, int(position_value / 100.0)),  # RiskManager adjusts
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        if snapshot.ticker != position.ticker:
            return False
        # Exit on stop-loss or take-profit
        if position.stop_loss and snapshot.price < position.stop_loss:
            return True
        if position.take_profit and snapshot.price >= position.take_profit:
            return True
        return False

    def build_exit_order(self, position: Position) -> Order:
        return Order(
            ticker=position.ticker,
            action="SELL",
            qty=abs(position.qty),
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def describe(self) -> str:
        return "MomentumBreakout: buys Stage 2 breakouts from scanner watchlist. Trail stop exit."


__all__ = ["MomentumBreakout"]
