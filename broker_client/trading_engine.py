"""TradingEngine orchestrator (Day 8, CLAUDE.md Section 9).

Receives signals → routes to enabled strategies → risk check → OrderRouter.
Does NOT make directional decisions — that's the strategy's job.
"""

from __future__ import annotations

from config.settings import settings
from core.logger import get_logger
from broker_client.order_manager import OrderManager
from broker_client.order_router import OrderRouter
from broker_client.risk_manager import RiskManager
from broker_client.strategies import load_enabled_strategies
from broker_client.strategies.base_strategy import BaseStrategy
from broker_core.base_broker import Account, Order, OptionsOrder
from signals.signal_schema import Signal

logger = get_logger(__name__)


class TradingEngine:
    """Orchestrates signal → strategy → risk → router pipeline."""

    def __init__(
        self,
        router: OrderRouter,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        account: Account,
        strategies: list[BaseStrategy] | None = None,
    ) -> None:
        self._router = router
        self._risk = risk_manager
        self._order_manager = order_manager
        self._account = account
        self._strategies = strategies or load_enabled_strategies(settings.ENABLED_STRATEGIES)

    def on_signal(self, signal: Signal) -> None:
        """Process a signal through all enabled strategies."""
        if self._risk.is_halted:
            logger.warning("TradingEngine: risk halt active — ignoring signal %s", signal.direction)
            return

        for strategy in self._strategies:
            if not strategy.should_enter(signal, self._account):
                continue

            order: Order | OptionsOrder = strategy.build_order(signal, self._account)

            equity_order = order if isinstance(order, Order) else None
            if not self._risk.approve(equity_order or order, self._account):  # type: ignore[arg-type]
                logger.info(
                    "TradingEngine: strategy=%s order rejected by risk manager",
                    strategy.strategy_name,
                )
                continue

            if isinstance(order, Order):
                result = self._router.execute(order)
            else:
                result = self._router.execute_options(order)
            self._order_manager.record(order, result)

            if result.status in ("filled", "partial"):
                logger.info(
                    "TradingEngine: strategy=%s ticker=%s result=%s fill=%.4f",
                    strategy.strategy_name, order.ticker, result.status, result.fill_price,
                )

    def update_account(self, account: Account) -> None:
        """Update the account snapshot used for sizing."""
        self._account = account

    @property
    def strategies(self) -> list[BaseStrategy]:
        return self._strategies


__all__ = ["TradingEngine"]
