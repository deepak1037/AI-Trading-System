"""Order router — single routing point for all orders (Day 6, CLAUDE.md Section 8).

TradingEngine always calls OrderRouter.execute() — never calls broker or
paper engine directly. Three routing modes: live, paper, backtest/development.
"""

from __future__ import annotations


from typing import cast

from config.settings import settings
from core.logger import get_logger
from broker_core.base_broker import BaseBroker, Order, OptionsOrder, OrderResult

logger = get_logger(__name__)


class OrderRouter:
    """Routes orders to the correct execution path based on ENV settings.

    Triple-gate for live orders (CLAUDE.md Section 2):
        ENV=live + DRY_RUN=False + LIVE_TRADING_ENABLED=True
    """

    def __init__(
        self,
        broker: BaseBroker,
        paper_engine=None,   # PaperTradingEngine | None
        paper_account=None,  # PaperAccount | None
    ) -> None:
        self._broker = broker
        self._paper_engine = paper_engine
        self._paper_account = paper_account

    def execute(self, order: Order) -> OrderResult:
        """Route an equity order to the appropriate execution path."""
        if (
            settings.ENV == "live"
            and not settings.DRY_RUN
            and settings.LIVE_TRADING_ENABLED
        ):
            logger.warning(
                "LIVE ORDER EXECUTING: %s %s x%d account=%s",
                order.action, order.ticker, order.qty, order.account_id,
            )
            return self._broker.place_order(order)

        elif settings.ENV == "paper":
            preview = self._broker.preview_order(order)
            if self._paper_account is not None:
                result = self._paper_account.apply_preview(order, preview)
                logger.info("PAPER ORDER (Schwab preview): %s %s x%d status=%s",
                            order.action, order.ticker, order.qty, result.status)
                return cast(OrderResult, result)
            # No paper account wired up — return the preview as a dry-run result
            return OrderResult(
                order_id="PAPER_NO_ACCOUNT",
                status="dry_run",
                fill_price=preview.estimated_cost / max(order.qty, 1),
            )

        elif settings.ENV == "backtest":
            if self._paper_engine is not None:
                result = self._paper_engine.simulate_fill(order)
                logger.debug("BACKTEST FILL: %s %s x%d price=%.4f",
                             order.action, order.ticker, order.qty, result.fill_price)
                return cast(OrderResult, result)
            return OrderResult(order_id="BACKTEST_NO_ENGINE", status="dry_run", fill_price=0.0)

        else:
            # development or any other ENV — log only
            logger.debug(
                "DEVELOPMENT MODE — order logged only, not submitted: %s %s x%d",
                order.action, order.ticker, order.qty,
            )
            return OrderResult(
                order_id="DRY_RUN",
                status="dry_run",
                fill_price=0.0,
                commission=0.0,
            )

    def execute_options(self, order: OptionsOrder) -> OrderResult:
        """Route an options order to the appropriate execution path."""
        if (
            settings.ENV == "live"
            and not settings.DRY_RUN
            and settings.LIVE_TRADING_ENABLED
        ):
            logger.warning(
                "LIVE OPTIONS ORDER: %s %s x%d contract=%s",
                order.action, order.ticker, order.qty, order.contract,
            )
            return self._broker.place_options_order(order)

        elif settings.ENV == "paper":
            self._broker.preview_options_order(order)
            if self._paper_engine is not None:
                return cast(OrderResult, self._paper_engine.simulate_options_fill(order))
            return OrderResult(
                order_id="PAPER_OPTIONS",
                status="dry_run",
                fill_price=order.limit_price or 0.0,
            )

        elif settings.ENV == "backtest":
            if self._paper_engine is not None:
                return cast(OrderResult, self._paper_engine.simulate_options_fill(order))
            return OrderResult(order_id="BACKTEST_OPTIONS", status="dry_run", fill_price=0.0)

        else:
            logger.debug("DEVELOPMENT MODE — options order not submitted: %s", order)
            return OrderResult(order_id="DRY_RUN", status="dry_run", fill_price=0.0)


__all__ = ["OrderRouter"]
