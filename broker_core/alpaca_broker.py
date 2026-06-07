"""Alpaca broker implementation (Day 6).

Uses alpaca-py. Paper mode when settings.ALPACA_PAPER=True.
"""

from __future__ import annotations

from typing import Any, Optional

from config.settings import settings
from core.exceptions import BrokerError, DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from broker_core.base_broker import (
    Account,
    BaseBroker,
    MarketHours,
    OptionsChain,
    OptionsOrder,
    Order,
    OrderPreview,
    OrderResult,
    OrderStatus,
    Position,
    Quote,
    Transaction,
)

logger = get_logger(__name__)


def _get_alpaca_client():
    """Lazy-load and return an Alpaca TradingClient."""
    try:
        from alpaca.trading.client import TradingClient  # type: ignore[import-untyped]
    except ImportError as exc:
        raise BrokerError("alpaca-py not installed") from exc
    if not settings.ALPACA_API_KEY:
        raise BrokerError("ALPACA_API_KEY not configured")
    return TradingClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )


class AlpacaBroker(BaseBroker):
    """Alpaca broker — paper or live via alpaca-py."""

    # ── Account ──────────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_account(self) -> Account:
        try:
            client = _get_alpaca_client()
            acct = client.get_account()
            return Account(
                account_id=str(acct.id),
                cash=float(acct.cash),
                equity=float(acct.equity),
                buying_power=float(acct.buying_power),
                margin_used=float(acct.initial_margin) if hasattr(acct, "initial_margin") else 0.0,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_account failed: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    def get_positions(self) -> list[Position]:
        try:
            client = _get_alpaca_client()
            raw = client.get_all_positions()
            positions = []
            for p in raw:
                positions.append(Position(
                    ticker=p.symbol,
                    qty=int(p.qty),
                    avg_cost=float(p.avg_entry_price),
                    current_price=float(p.current_price) if p.current_price else 0.0,
                    unrealized_pnl=float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                ))
            return positions
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_positions failed: {exc}") from exc

    def get_transactions(self, days: int = 30) -> list[Transaction]:
        # TODO: Alpaca portfolio history not directly exposed as transactions list
        logger.warning("get_transactions not fully implemented for Alpaca")
        return []

    # ── Orders ───────────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def place_order(self, order: Order) -> OrderResult:
        try:
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest  # type: ignore[import-untyped]
            from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore[import-untyped]

            client = _get_alpaca_client()
            side = OrderSide.BUY if order.action in ("BUY",) else OrderSide.SELL
            tif = TimeInForce.DAY if order.time_in_force == "day" else TimeInForce.GTC

            if order.order_type == "market":
                req: MarketOrderRequest | LimitOrderRequest = MarketOrderRequest(
                    symbol=order.ticker, qty=order.qty, side=side, time_in_force=tif)
            else:
                req = LimitOrderRequest(
                    symbol=order.ticker, qty=order.qty, side=side,
                    time_in_force=tif, limit_price=order.limit_price
                )
            result = client.submit_order(req)
            return OrderResult(
                order_id=str(result.id),
                status="pending",
                fill_price=0.0,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"place_order failed: {exc}") from exc

    def preview_order(self, order: Order) -> OrderPreview:
        try:
            acct = self.get_account()
            estimated_cost = (order.limit_price or 0.0) * order.qty
            return OrderPreview(
                estimated_cost=estimated_cost,
                buying_power_effect=-estimated_cost,
                is_valid=estimated_cost <= acct.buying_power,
            )
        except Exception as exc:
            return OrderPreview(is_valid=False, rejection_reason=str(exc))

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    def cancel_order(self, order_id: str) -> bool:
        try:
            client = _get_alpaca_client()
            client.cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            raise BrokerError(f"cancel_order failed: {exc}") from exc

    def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            client = _get_alpaca_client()
            o = client.get_order_by_id(order_id)
            status_map = {
                "new": "pending", "accepted": "pending",
                "filled": "filled", "partially_filled": "partial",
                "canceled": "cancelled", "rejected": "rejected",
            }
            return status_map.get(str(o.status), "pending")  # type: ignore[return-value]
        except Exception as exc:
            raise BrokerError(f"get_order_status failed: {exc}") from exc

    def get_order_history(self, days: int = 30) -> list[Order]:
        # Simplified — returns empty list; expand as needed
        return []

    def place_options_order(self, order: OptionsOrder) -> OrderResult:
        # TODO: Alpaca options trading requires options account approval
        raise BrokerError("Options trading not supported on this Alpaca account tier")

    def preview_options_order(self, order: OptionsOrder) -> OrderPreview:
        return OrderPreview(is_valid=False, rejection_reason="Options not supported on Alpaca")

    # ── Market data ───────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(DataError,))
    def get_quote(self, ticker: str) -> Quote:
        try:
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import StockLatestQuoteRequest  # type: ignore[import-untyped]

            data_client = StockHistoricalDataClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
            )
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quotes = data_client.get_stock_latest_quote(req)
            q = quotes.get(ticker)
            if q is None:
                raise DataError(f"No quote for {ticker}")
            return Quote(
                ticker=ticker,
                bid=float(q.bid_price) if q.bid_price else 0.0,
                ask=float(q.ask_price) if q.ask_price else 0.0,
                last=float(q.ask_price) if q.ask_price else 0.0,
                volume=0,
                timestamp=q.timestamp,
            )
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"get_quote failed: {exc}") from exc

    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> OptionsChain:
        raise DataError("Options chain not available on basic Alpaca plan")

    def get_market_hours(self) -> MarketHours:
        try:
            client = _get_alpaca_client()
            clock = client.get_clock()
            return MarketHours(
                is_open=bool(clock.is_open),
                market_open=clock.next_open,
                market_close=clock.next_close,
            )
        except Exception as exc:
            raise BrokerError(f"get_market_hours failed: {exc}") from exc

    def stream_quotes(self, tickers: list[str], callback: Any) -> None:
        # TODO: Implement streaming with alpaca-py WebSocket
        logger.warning("stream_quotes not yet implemented for AlpacaBroker")


__all__ = ["AlpacaBroker"]
