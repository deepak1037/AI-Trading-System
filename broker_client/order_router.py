"""Order router — single routing point for all orders (Day 6, CLAUDE.md Section 8).

TradingEngine always calls OrderRouter.execute() — never calls broker or
paper engine directly. Three routing modes: live, paper, backtest/development.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from broker_core.base_broker import (
    BaseBroker,
    OptionsOrder,
    Order,
    OrderAction,
    OrderResult,
)
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


def _get(pos: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or an attribute-bearing object."""
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


class LadderLeg(BaseModel):
    """One tranche of a scale-out (ladder) exit."""

    tranche: int
    contracts: int
    kind: str                      # "close" | "deferred" | "trailing"
    status: str                    # filled | dry_run | pending | armed
    order_id: str | None = None
    trigger_profit_pct: float | None = None
    stop_at_pct: float | None = None
    description: str = ""


class OrderRouter:
    """Routes orders to the correct execution path based on ENV settings.

    Triple-gate for live orders (CLAUDE.md Section 2):
        ENV=live + DRY_RUN=False + LIVE_TRADING_ENABLED=True
    """

    def __init__(
        self,
        broker: BaseBroker,
        paper_engine: Any = None,   # PaperTradingEngine | None
        paper_account: Any = None,  # PaperAccount | None
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

    # ── Scale-out (ladder) execution (Phase 2 Step 7) ─────────────────────────

    def execute_ladder(
        self,
        position: Any,
        tranches: list[float] | None = None,
    ) -> list[LadderLeg]:
        """Close a position in tranches (default 30% now / 40% target / 30% ride).

        Tranche 0 closes immediately; tranche 1 is a deferred exit armed at a
        higher profit target; the final tranche rides with a breakeven stop.
        ``tranches`` must sum to 1.0. Paper-trading only — the deferred and
        trailing legs are armed descriptors, not live resting orders.
        """
        tranches = tranches or list(settings.LADDER_DEFAULT_TRANCHES)
        if abs(sum(tranches) - 1.0) > 1e-6:
            raise ValueError(f"Ladder tranches must sum to 1.0, got {sum(tranches)}")

        total = int(_get(position, "qty", _get(position, "quantity", 0)) or 0)
        if total <= 0:
            raise ValueError("Cannot ladder a position with non-positive quantity")

        legs: list[LadderLeg] = []
        allocated = 0
        last = len(tranches) - 1
        for i, pct in enumerate(tranches):
            # Final tranche absorbs rounding remainder so contracts sum exactly.
            contracts = (total - allocated) if i == last else max(1, int(total * pct))
            contracts = min(contracts, total - allocated)
            allocated += contracts
            if contracts <= 0:
                continue
            if i == 0:
                legs.append(self._ladder_close_now(position, contracts))
            elif i == 1:
                legs.append(self._create_deferred_exit(
                    position, contracts,
                    trigger_profit_pct=settings.LADDER_SECOND_TARGET_PROFIT_PCT,
                ))
            else:
                legs.append(self._create_trailing_stop(
                    position, contracts, stop_at_pct=settings.LADDER_TRAILING_STOP_PCT,
                ))
        logger.info(
            "OrderRouter: laddered %s into %d legs (%s)",
            _get(position, "ticker", "?"), len(legs), tranches,
        )
        return legs

    def close_position(self, position: Any, quantity: int) -> OrderResult:
        """Route a closing order for ``quantity`` units/contracts of a position."""
        ticker = _get(position, "ticker", "?")
        strategy = _get(position, "strategy", _get(position, "strategy_name", "ladder"))
        option_type = _get(position, "option_type")
        contract = _get(position, "contract")
        position_type = str(_get(position, "position_type", "equity_long"))
        is_short = position_type.endswith("short")

        if option_type or contract:
            opt_action: OrderAction = "BUY_TO_CLOSE" if is_short else "SELL_TO_CLOSE"
            opt_order = OptionsOrder(
                ticker=ticker,
                action=opt_action,
                contract=str(contract or ticker),
                qty=quantity,
                order_type="market",
                strategy_name=strategy,
            )
            return self.execute_options(opt_order)

        eq_action: OrderAction = "BUY" if is_short else "SELL"
        eq_order = Order(
            ticker=ticker,
            action=eq_action,
            qty=quantity,
            order_type="market",
            strategy_name=strategy,
        )
        return self.execute(eq_order)

    def _ladder_close_now(self, position: Any, contracts: int) -> LadderLeg:
        result = self.close_position(position, contracts)
        return LadderLeg(
            tranche=0,
            contracts=contracts,
            kind="close",
            status=result.status,
            order_id=result.order_id,
            description=f"Close {contracts} now",
        )

    def _create_deferred_exit(
        self, position: Any, contracts: int, trigger_profit_pct: float,
    ) -> LadderLeg:
        """Arm a deferred exit that fires when profit reaches the trigger."""
        logger.info(
            "OrderRouter: armed deferred exit %s x%d @ %.0f%% profit",
            _get(position, "ticker", "?"), contracts, trigger_profit_pct,
        )
        return LadderLeg(
            tranche=1,
            contracts=contracts,
            kind="deferred",
            status="armed",
            trigger_profit_pct=trigger_profit_pct,
            description=f"Close {contracts} at {trigger_profit_pct:.0f}% profit",
        )

    def _create_trailing_stop(
        self, position: Any, contracts: int, stop_at_pct: float,
    ) -> LadderLeg:
        """Arm a trailing stop for the final tranche (default breakeven)."""
        logger.info(
            "OrderRouter: armed trailing stop %s x%d @ %.0f%%",
            _get(position, "ticker", "?"), contracts, stop_at_pct,
        )
        return LadderLeg(
            tranche=2,
            contracts=contracts,
            kind="trailing",
            status="armed",
            stop_at_pct=stop_at_pct,
            description=f"Ride {contracts} with stop at {stop_at_pct:.0f}%",
        )


__all__ = ["OrderRouter", "LadderLeg"]
