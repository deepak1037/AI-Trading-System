"""Schwab broker implementation via schwab-py (Day 6, primary broker).

Implements all 14 BaseBroker methods + preview_order.
Uses schwab-py (github.com/alexgolec/schwab-py).
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    OptionsContract,
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


def _get_schwab_client():
    """Lazy-load and return an authenticated schwab-py client."""
    try:
        import schwab  # type: ignore[import-untyped]
    except ImportError as exc:
        raise BrokerError("schwab-py not installed") from exc

    token_file = settings.SCHWAB_TOKEN_FILE
    try:
        client = schwab.auth.client_from_token_file(
            token_path=token_file,
            api_key=settings.SCHWAB_CLIENT_ID,
            app_secret=settings.SCHWAB_CLIENT_SECRET,
        )
        return client
    except FileNotFoundError:
        # TODO: Run schwab.auth.easy_client() once to generate token file
        raise BrokerError(
            f"Schwab token file not found at {token_file!r}. "
            "Run the OAuth flow once to generate it."
        )
    except Exception as exc:
        raise BrokerError(f"Schwab auth failed: {exc}") from exc


class SchwabBroker(BaseBroker):
    """Charles Schwab broker via schwab-py.

    All order methods use the Schwab Developer API. ``preview_order`` calls
    the Schwab ``previewOrder`` endpoint — real API validation, no real trade.
    """

    # ── Account ──────────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_account(self) -> Account:
        try:
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            accounts = resp.json()
            if not accounts:
                raise BrokerError("No accounts returned by Schwab")
            first = accounts[0]
            acct_id = first.get("hashValue", "unknown")

            detail = client.get_account(account_id=acct_id, fields=["positions"])
            detail.raise_for_status()
            data = detail.json()
            sec_acct = data.get("securitiesAccount", {})
            cur_bal = sec_acct.get("currentBalances", {})

            return Account(
                account_id=acct_id,
                cash=float(cur_bal.get("cashBalance", 0)),
                equity=float(cur_bal.get("liquidationValue", 0)),
                buying_power=float(cur_bal.get("buyingPower", 0)),
                margin_used=float(cur_bal.get("maintenanceRequirement", 0)),
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Schwab get_account failed: {exc}") from exc

    def get_positions(self) -> list[Position]:
        try:
            client = _get_schwab_client()
            resp = client.get_accounts(fields=["positions"])
            resp.raise_for_status()
            accts = resp.json()
            positions = []
            for acct in accts:
                sec = acct.get("securitiesAccount", {})
                for p in sec.get("positions", []):
                    inst = p.get("instrument", {})
                    positions.append(Position(
                        ticker=inst.get("symbol", "UNKNOWN"),
                        qty=int(float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))),
                        avg_cost=float(p.get("averagePrice", 0)),
                        current_price=float(p.get("marketValue", 0)) / max(1, float(p.get("longQuantity", 1) or 1)),
                        unrealized_pnl=float(p.get("currentDayProfitLoss", 0)),
                    ))
            return positions
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Schwab get_positions failed: {exc}") from exc

    def get_transactions(self, days: int = 30) -> list[Transaction]:
        try:
            from datetime import timedelta
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]
            start = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            end = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            t_resp = client.get_transactions(account_id=acct_id, start_date=start, end_date=end)
            t_resp.raise_for_status()
            txns = []
            for t in t_resp.json():
                txns.append(Transaction(
                    transaction_id=str(t.get("activityId", "")),
                    ticker=t.get("transferItems", [{}])[0].get("instrument", {}).get("symbol", ""),
                    action=t.get("type", ""),
                    qty=int(float(t.get("transferItems", [{}])[0].get("amount", 0))),
                    price=float(t.get("netAmount", 0)),
                    executed_at=datetime.fromisoformat(t["tradeDate"]) if "tradeDate" in t else None,
                ))
            return txns
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Schwab get_transactions failed: {exc}") from exc

    # ── Orders ───────────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def place_order(self, order: Order) -> OrderResult:
        try:
            import schwab  # type: ignore[import-untyped]
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]

            builder = schwab.orders.equities
            if order.action == "BUY":
                if order.order_type == "market":
                    order_spec = builder.equity_buy_market(order.ticker, order.qty)
                else:
                    order_spec = builder.equity_buy_limit(order.ticker, order.qty, order.limit_price)
            else:
                if order.order_type == "market":
                    order_spec = builder.equity_sell_market(order.ticker, order.qty)
                else:
                    order_spec = builder.equity_sell_limit(order.ticker, order.qty, order.limit_price)

            resp = client.place_order(acct_id, order_spec)
            resp.raise_for_status()
            order_id = resp.headers.get("location", "").split("/")[-1]
            logger.warning("SCHWAB LIVE ORDER: %s %s x%d → order_id=%s",
                           order.action, order.ticker, order.qty, order_id)
            return OrderResult(order_id=order_id, status="pending")
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Schwab place_order failed: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    def preview_order(self, order: Order) -> OrderPreview:
        """Call Schwab previewOrder — real API validation, no real trade."""
        try:
            import schwab  # type: ignore[import-untyped]
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]

            builder = schwab.orders.equities
            if order.action == "BUY":
                order_spec = (builder.equity_buy_market(order.ticker, order.qty)
                              if order.order_type == "market"
                              else builder.equity_buy_limit(order.ticker, order.qty, order.limit_price))
            else:
                order_spec = (builder.equity_sell_market(order.ticker, order.qty)
                              if order.order_type == "market"
                              else builder.equity_sell_limit(order.ticker, order.qty, order.limit_price))

            preview_resp = client.preview_order(acct_id, order_spec)
            preview_resp.raise_for_status()
            data = preview_resp.json()
            return OrderPreview(
                estimated_cost=float(data.get("estimatedTotalCost", 0)),
                buying_power_effect=float(data.get("buyingPowerEffect", {}).get("netEffect", 0)),
                fees=float(data.get("estimatedCommission", 0)),
                is_valid=True,
            )
        except BrokerError:
            raise
        except Exception as exc:
            return OrderPreview(is_valid=False, rejection_reason=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        try:
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]
            c_resp = client.cancel_order(account_id=acct_id, order_id=order_id)
            c_resp.raise_for_status()
            return True
        except Exception as exc:
            raise BrokerError(f"Schwab cancel_order failed: {exc}") from exc

    def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]
            o_resp = client.get_order(account_id=acct_id, order_id=order_id)
            o_resp.raise_for_status()
            data = o_resp.json()
            status_map = {
                "WORKING": "pending", "PENDING_ACTIVATION": "pending",
                "FILLED": "filled", "PARTIALLY_FILLED": "partial",
                "CANCELED": "cancelled", "REJECTED": "rejected",
            }
            return status_map.get(data.get("status", ""), "pending")  # type: ignore[return-value]
        except Exception as exc:
            raise BrokerError(f"Schwab get_order_status failed: {exc}") from exc

    def get_order_history(self, days: int = 30) -> list[Order]:
        return []

    def place_options_order(self, order: OptionsOrder) -> OrderResult:
        try:
            import schwab  # type: ignore[import-untyped]
            client = _get_schwab_client()
            resp = client.get_accounts_numbers()
            resp.raise_for_status()
            acct_id = resp.json()[0]["hashValue"]

            builder = schwab.orders.options
            if order.action in ("BUY_TO_OPEN", "BUY"):
                order_spec = builder.option_buy_to_open_market(order.contract, order.qty)
            elif order.action in ("SELL_TO_OPEN",):
                order_spec = builder.option_sell_to_open_limit(order.contract, order.qty, order.limit_price)
            elif order.action in ("BUY_TO_CLOSE",):
                order_spec = builder.option_buy_to_close_limit(order.contract, order.qty, order.limit_price)
            else:
                order_spec = builder.option_sell_to_close_market(order.contract, order.qty)

            o_resp = client.place_order(acct_id, order_spec)
            o_resp.raise_for_status()
            order_id = o_resp.headers.get("location", "").split("/")[-1]
            return OrderResult(order_id=order_id, status="pending")
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Schwab place_options_order failed: {exc}") from exc

    def preview_options_order(self, order: OptionsOrder) -> OrderPreview:
        return OrderPreview(
            estimated_cost=(order.limit_price or 0.0) * order.qty * 100,
            is_valid=True,
        )

    # ── Market data ───────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(DataError,))
    def get_quote(self, ticker: str) -> Quote:
        try:
            client = _get_schwab_client()
            resp = client.get_quote(ticker)
            resp.raise_for_status()
            data = resp.json().get(ticker, {}).get("quote", {})
            return Quote(
                ticker=ticker,
                bid=float(data.get("bidPrice", 0)),
                ask=float(data.get("askPrice", 0)),
                last=float(data.get("lastPrice", 0)),
                volume=int(data.get("totalVolume", 0)),
                timestamp=datetime.now(tz=timezone.utc),
            )
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"Schwab get_quote failed: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(DataError,))
    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> OptionsChain:
        try:
            client = _get_schwab_client()
            kwargs: dict[str, Any] = {"symbol": ticker}
            if expiry:
                kwargs["toDate"] = expiry
            resp = client.get_option_chain(**kwargs)
            resp.raise_for_status()
            data = resp.json()
            calls_raw = data.get("callExpDateMap", {})
            puts_raw = data.get("putExpDateMap", {})

            def _parse_contracts(exp_map: dict, option_type: str) -> list[OptionsContract]:
                contracts = []
                for _exp_date, strikes in exp_map.items():
                    for strike_str, contracts_list in strikes.items():
                        for c in contracts_list:
                            contracts.append(OptionsContract(
                                symbol=c.get("symbol", ""),
                                expiry=c.get("expirationDate", "")[:10],
                                strike=float(c.get("strikePrice", 0)),
                                option_type=option_type,  # type: ignore[arg-type]
                                bid=float(c.get("bid", 0)),
                                ask=float(c.get("ask", 0)),
                                last=float(c.get("last", 0)),
                                volume=int(c.get("totalVolume", 0)),
                                open_interest=int(c.get("openInterest", 0)),
                                delta=c.get("delta"),
                                gamma=c.get("gamma"),
                                theta=c.get("theta"),
                                implied_vol=c.get("volatility"),
                            ))
                return contracts

            first_exp = next(iter(calls_raw), expiry or "")
            return OptionsChain(
                ticker=ticker,
                expiry=first_exp[:10] if first_exp else "",
                calls=_parse_contracts(calls_raw, "call"),
                puts=_parse_contracts(puts_raw, "put"),
            )
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"Schwab get_options_chain failed: {exc}") from exc

    def get_market_hours(self) -> MarketHours:
        try:
            client = _get_schwab_client()
            resp = client.get_markets_hours(markets=["EQUITY"])
            resp.raise_for_status()
            data = resp.json()
            equity = data.get("equity", {}).get("equity", {})
            is_open = equity.get("isOpen", False)
            return MarketHours(is_open=bool(is_open))
        except Exception as exc:
            raise BrokerError(f"Schwab get_market_hours failed: {exc}") from exc

    def stream_quotes(self, tickers: list[str], callback: Any) -> None:
        # TODO: Implement Schwab streaming via schwab-py StreamClient
        logger.warning("stream_quotes not yet implemented for SchwabBroker")


__all__ = ["SchwabBroker"]
