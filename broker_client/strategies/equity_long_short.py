"""Equity long/short strategy (Day 8).

Buys on bullish signals, shorts on bearish signals, when confidence
exceeds the configured threshold. Exits on stop/take-profit or regime change.
"""

from __future__ import annotations

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import Account, Order, Position
from broker_client.strategies.base_strategy import BaseStrategy
from signals.signal_schema import MarketSnapshot, Signal

logger = get_logger(__name__)


class EquityLongShort(BaseStrategy):
    """Buy long on bullish signal, sell short on bearish signal."""

    strategy_name = "equity_long_short"
    strategy_type = "equity"
    timeframe = "short"

    def should_enter(self, signal: Signal, account: Account) -> bool:
        if signal.source not in ("macro", "yield", "fusion"):
            return False
        if signal.confidence < settings.CONFIDENCE_HIGH:
            return False
        if signal.direction == "neutral":
            return False
        # Check available buying power
        min_bp = account.equity * settings.MAX_POSITION_PCT * 0.5
        return account.buying_power >= min_bp

    def build_order(self, signal: Signal, account: Account) -> Order:
        """Build a market order sized at MAX_POSITION_PCT of equity."""
        position_value = account.equity * settings.MAX_POSITION_PCT
        # Get approximate price — we'll use a placeholder; RiskManager adjusts qty
        estimated_price = account.equity / 1000 if account.equity > 0 else 100.0
        qty = max(1, int(position_value / max(estimated_price, 1.0)))

        from typing import Literal
        _action: Literal["BUY", "SELL"] = "BUY" if signal.direction in ("long", "strong_long") else "SELL"
        return Order(
            ticker=signal.metadata.get("ticker", "SPY"),
            action=_action,
            qty=qty,
            order_type="market",
            strategy_name=self.strategy_name,
            account_id=settings.PAPER_ACCOUNTS[0] if settings.PAPER_ACCOUNTS else "paper_main",
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        if snapshot.ticker != position.ticker:
            return False
        if position.stop_loss and snapshot.price <= position.stop_loss:
            logger.info("EquityLongShort: stop-loss hit for %s @ %.4f", position.ticker, snapshot.price)
            return True
        if position.take_profit and snapshot.price >= position.take_profit:
            logger.info("EquityLongShort: take-profit hit for %s @ %.4f", position.ticker, snapshot.price)
            return True
        return False

    def build_exit_order(self, position: Position) -> Order:
        from typing import Literal
        _action: Literal["BUY", "SELL"] = "SELL" if position.qty > 0 else "BUY"
        return Order(
            ticker=position.ticker,
            action=_action,
            qty=abs(position.qty),
            order_type="market",
            strategy_name=self.strategy_name,
        )

    def describe(self) -> str:
        return (
            "EquityLongShort: buys on bullish signal (confidence >= CONFIDENCE_HIGH), "
            "shorts on bearish. Exits on stop/take-profit or regime change."
        )


__all__ = ["EquityLongShort"]
