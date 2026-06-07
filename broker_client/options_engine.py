"""Options engine — chain fetch, strike selection, spread construction (Day 10)."""

from __future__ import annotations

from typing import Optional

from core.exceptions import DataError
from core.logger import get_logger
from broker_core.base_broker import BaseBroker, OptionsChain, OptionsContract, OptionsOrder

logger = get_logger(__name__)


class OptionsEngine:
    """Helpers for selecting strikes and constructing option orders.

    Used by options strategies (CoveredCall, CashSecuredPut, etc.).
    """

    def __init__(self, broker: Optional[BaseBroker] = None) -> None:
        self._broker = broker

    def fetch_chain(
        self, ticker: str, expiry: Optional[str] = None
    ) -> OptionsChain:
        """Fetch the options chain from the broker."""
        if self._broker is None:
            raise DataError("No broker configured for OptionsEngine")
        return self._broker.get_options_chain(ticker, expiry=expiry)

    def select_otm_call(
        self,
        chain: OptionsChain,
        current_price: float,
        otm_pct: float = 0.05,
    ) -> Optional[OptionsContract]:
        """Return the OTM call strike closest to current_price * (1 + otm_pct).

        Args:
            chain: Full options chain.
            current_price: Current underlying price.
            otm_pct: How far OTM (default 5%).

        Returns:
            OptionsContract or None if chain is empty.
        """
        target_strike = current_price * (1 + otm_pct)
        calls = [c for c in chain.calls if c.strike >= current_price]
        if not calls:
            return None
        return min(calls, key=lambda c: abs(c.strike - target_strike))

    def select_otm_put(
        self,
        chain: OptionsChain,
        current_price: float,
        otm_pct: float = 0.05,
    ) -> Optional[OptionsContract]:
        """Return the OTM put strike closest to current_price * (1 - otm_pct).

        Args:
            chain: Full options chain.
            current_price: Current underlying price.
            otm_pct: How far OTM (default 5%).

        Returns:
            OptionsContract or None if chain is empty.
        """
        target_strike = current_price * (1 - otm_pct)
        puts = [p for p in chain.puts if p.strike <= current_price]
        if not puts:
            return None
        return min(puts, key=lambda p: abs(p.strike - target_strike))

    def select_delta_call(
        self,
        chain: OptionsChain,
        target_delta: float = 0.30,
    ) -> Optional[OptionsContract]:
        """Return the call contract closest to a target delta value."""
        calls_with_delta = [c for c in chain.calls if c.delta is not None]
        if not calls_with_delta:
            return None
        return min(calls_with_delta, key=lambda c: abs((c.delta or 0) - target_delta))

    def select_delta_put(
        self,
        chain: OptionsChain,
        target_delta: float = -0.30,
    ) -> Optional[OptionsContract]:
        """Return the put contract closest to a target delta value."""
        puts_with_delta = [p for p in chain.puts if p.delta is not None]
        if not puts_with_delta:
            return None
        return min(puts_with_delta, key=lambda p: abs((p.delta or 0) - target_delta))

    @staticmethod
    def mid_price(contract: OptionsContract) -> float:
        """Return the mid-price (bid + ask) / 2."""
        if contract.bid and contract.ask:
            return (contract.bid + contract.ask) / 2
        return contract.last or 0.0

    def build_sell_call_order(
        self,
        ticker: str,
        contract: OptionsContract,
        qty: int = 1,
        strategy_name: str = "covered_call",
    ) -> OptionsOrder:
        return OptionsOrder(
            ticker=ticker,
            action="SELL_TO_OPEN",
            contract=contract.symbol,
            qty=qty,
            order_type="limit",
            limit_price=self.mid_price(contract),
            strategy_name=strategy_name,
        )

    def build_sell_put_order(
        self,
        ticker: str,
        contract: OptionsContract,
        qty: int = 1,
        strategy_name: str = "cash_secured_put",
    ) -> OptionsOrder:
        return OptionsOrder(
            ticker=ticker,
            action="SELL_TO_OPEN",
            contract=contract.symbol,
            qty=qty,
            order_type="limit",
            limit_price=self.mid_price(contract),
            strategy_name=strategy_name,
        )

    def build_buy_put_order(
        self,
        ticker: str,
        contract: OptionsContract,
        qty: int = 1,
        strategy_name: str = "protective_put",
    ) -> OptionsOrder:
        return OptionsOrder(
            ticker=ticker,
            action="BUY_TO_OPEN",
            contract=contract.symbol,
            qty=qty,
            order_type="limit",
            limit_price=self.mid_price(contract),
            strategy_name=strategy_name,
        )


__all__ = ["OptionsEngine"]
