"""IBKR broker stub — not yet implemented (Day 6)."""

from __future__ import annotations

from typing import Any, Optional

from core.exceptions import BrokerError
from broker_core.base_broker import (
    Account, BaseBroker, MarketHours, OptionsChain, OptionsOrder,
    Order, OrderPreview, OrderResult, OrderStatus, Position, Quote, Transaction,
)


class IBKRBroker(BaseBroker):
    """Interactive Brokers stub. TODO: Implement using ib_insync or ibapi."""

    def _not_implemented(self, method: str):
        raise BrokerError(f"IBKRBroker.{method} not yet implemented")

    def get_account(self) -> Account: return self._not_implemented("get_account")  # type: ignore
    def get_positions(self) -> list[Position]: return self._not_implemented("get_positions")  # type: ignore
    def get_transactions(self, days: int = 30) -> list[Transaction]: return self._not_implemented("get_transactions")  # type: ignore
    def place_order(self, order: Order) -> OrderResult: return self._not_implemented("place_order")  # type: ignore
    def preview_order(self, order: Order) -> OrderPreview: return self._not_implemented("preview_order")  # type: ignore
    def cancel_order(self, order_id: str) -> bool: return self._not_implemented("cancel_order")  # type: ignore
    def get_order_status(self, order_id: str) -> OrderStatus: return self._not_implemented("get_order_status")  # type: ignore
    def get_order_history(self, days: int = 30) -> list[Order]: return self._not_implemented("get_order_history")  # type: ignore
    def place_options_order(self, order: OptionsOrder) -> OrderResult: return self._not_implemented("place_options_order")  # type: ignore
    def preview_options_order(self, order: OptionsOrder) -> OrderPreview: return self._not_implemented("preview_options_order")  # type: ignore
    def get_quote(self, ticker: str) -> Quote: return self._not_implemented("get_quote")  # type: ignore
    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> OptionsChain: return self._not_implemented("get_options_chain")  # type: ignore
    def get_market_hours(self) -> MarketHours: return self._not_implemented("get_market_hours")  # type: ignore
    def stream_quotes(self, tickers: list[str], callback: Any) -> None: self._not_implemented("stream_quotes")


__all__ = ["IBKRBroker"]
