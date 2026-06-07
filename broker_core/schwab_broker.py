"""
broker_core/schwab_broker.py

Production Schwab broker implementation via schwab-py.
Implements all 14 BaseBroker abstract methods + preview_order.

Key design decisions:
- Account hash resolved ONCE at __init__ and cached — never per call
- Options orders ALWAYS require limit_price — market orders blocked
- preview_options_order calls real Schwab previewOrder API — not a stub
- All errors raised as BrokerError (never swallowed, never print())
- @retry + @circuit_breaker on every external call, params from settings
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

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


# ── Module-level helpers ───────────────────────────────────────────────────────


def _import_schwab():
    """Lazy import schwab-py — raises BrokerError if not installed."""
    try:
        import schwab  # type: ignore[import-untyped]
        return schwab
    except ImportError as exc:
        raise BrokerError(
            "schwab-py not installed. Run: pip install schwab-py"
        ) from exc


def _parse_position_type(instrument: dict, long_qty: float, short_qty: float) -> str:
    """Derive human-readable position type from Schwab instrument dict."""
    asset = instrument.get("assetType", "EQUITY")
    if asset == "OPTION":
        put_call = instrument.get("putCall", "unknown").lower()
        return f"option_{put_call}"
    return "equity_long" if long_qty > 0 else "equity_short"


def _parse_option_contracts(exp_date_map: dict, option_type: str) -> list[OptionsContract]:
    """Parse Schwab callExpDateMap / putExpDateMap into OptionsContract list."""
    contracts = []
    for exp_date_str, strikes in exp_date_map.items():
        # exp_date_str format: "2026-06-20:7" — split off the DTE suffix
        expiry_str = exp_date_str.split(":")[0]
        for _strike_str, options_list in strikes.items():
            for opt in options_list:
                contracts.append(
                    OptionsContract(
                        symbol        = opt.get("symbol", ""),
                        expiry        = expiry_str,
                        strike        = float(opt.get("strikePrice", 0.0)),
                        option_type   = option_type,
                        bid           = float(opt.get("bid", 0.0)),
                        ask           = float(opt.get("ask", 0.0)),
                        last          = float(opt.get("last", 0.0)),
                        volume        = int(opt.get("totalVolume", 0)),
                        open_interest = int(opt.get("openInterest", 0)),
                        delta         = opt.get("delta"),
                        gamma         = opt.get("gamma"),
                        theta         = opt.get("theta"),
                        implied_vol   = opt.get("volatility"),
                    )
                )
    return contracts


# ── SchwabBroker ──────────────────────────────────────────────────────────────


class SchwabBroker(BaseBroker):
    """
    Charles Schwab broker via schwab-py (github.com/alexgolec/schwab-py).

    Lifecycle
    ---------
    1. __init__ authenticates and caches the account hash ONCE.
    2. All public methods use self._account_hash — no per-call hash resolution.
    3. All external calls decorated with @retry + @circuit_breaker from settings.
    4. All errors raised as BrokerError — CRITICAL severity triggers SMS alert.
    5. No print() anywhere — structured logger only.
    """

    def __init__(self) -> None:
        self._schwab = _import_schwab()
        self._client = self._authenticate()
        self._account_hash: str = self._resolve_account_hash()
        logger.info(
            "SchwabBroker ready | account=...%s",
            str(settings.SCHWAB_ACCOUNT_NUMBER)[-4:],
        )

    # ── Authentication ────────────────────────────────────────────────────────

    def _authenticate(self):
        """
        Try token file first — fall back to interactive OAuth login flow.
        Token is auto-refreshed by schwab-py on expiry.
        """
        try:
            client = self._schwab.auth.client_from_token_file(
                token_path = settings.SCHWAB_TOKEN_FILE,
                api_key    = settings.SCHWAB_CLIENT_ID,
                app_secret = settings.SCHWAB_CLIENT_SECRET,
            )
            logger.info("Schwab auth via token file: %s", settings.SCHWAB_TOKEN_FILE)
            return client
        except FileNotFoundError:
            logger.warning(
                "Token file not found (%s) — starting OAuth login flow",
                settings.SCHWAB_TOKEN_FILE,
            )
            try:
                client = self._schwab.auth.client_from_login_flow(
                    api_key      = settings.SCHWAB_CLIENT_ID,
                    app_secret   = settings.SCHWAB_CLIENT_SECRET,
                    callback_url = settings.SCHWAB_REDIRECT_URI,
                    token_path   = settings.SCHWAB_TOKEN_FILE,
                )
                logger.info("OAuth complete — token saved to %s", settings.SCHWAB_TOKEN_FILE)
                return client
            except Exception as exc:
                raise BrokerError(
                    f"Schwab OAuth login flow failed: {exc}",
                    severity="CRITICAL",
                ) from exc
        except Exception as exc:
            raise BrokerError(
                f"Schwab authentication failed: {exc}",
                severity="CRITICAL",
            ) from exc

    def _resolve_account_hash(self) -> str:
        """
        Resolve SCHWAB_ACCOUNT_NUMBER to Schwab hash value.
        Called ONCE at __init__ and cached as self._account_hash.
        Never called again — all methods use self._account_hash directly.
        Raises BrokerError(CRITICAL) if account not found.
        """
        try:
            resp = self._client.get_account_numbers()
            resp.raise_for_status()
            accounts = resp.json()
        except Exception as exc:
            raise BrokerError(
                f"Failed to fetch Schwab account numbers: {exc}",
                severity="CRITICAL",
            ) from exc

        for acct in accounts:
            if acct["accountNumber"] == str(settings.SCHWAB_ACCOUNT_NUMBER):
                logger.debug(
                    "Account hash resolved for ...%s",
                    str(settings.SCHWAB_ACCOUNT_NUMBER)[-4:],
                )
                return acct["hashValue"]

        raise BrokerError(
            f"Account {settings.SCHWAB_ACCOUNT_NUMBER} not found in Schwab accounts. "
            f"Available accounts: {[a['accountNumber'] for a in accounts]}",
            severity="CRITICAL",
        )

    # ── Account ───────────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_account(self) -> Account:
        """Return current balances and buying power."""
        try:
            resp = self._client.get_account(
                account_hash = self._account_hash,
                fields       = [self._client.Account.Fields.POSITIONS],
            )
            resp.raise_for_status()
            bal = resp.json().get("securitiesAccount", {}).get("currentBalances", {})
            return Account(
                account_id   = str(settings.SCHWAB_ACCOUNT_NUMBER),
                cash         = float(bal.get("cashBalance", 0.0)),
                equity       = float(bal.get("liquidationValue", 0.0)),
                buying_power = float(bal.get("buyingPower", 0.0)),
                margin_used  = float(bal.get("maintenanceRequirement", 0.0)),
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_account failed: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_positions(self) -> list[Position]:
        """Return all open positions for the configured account."""
        try:
            resp = self._client.get_account(
                account_hash = self._account_hash,
                fields       = [self._client.Account.Fields.POSITIONS],
            )
            resp.raise_for_status()
            raw = resp.json().get("securitiesAccount", {}).get("positions", [])
            positions = []
            for p in raw:
                inst      = p.get("instrument", {})
                long_qty  = float(p.get("longQuantity", 0))
                short_qty = float(p.get("shortQuantity", 0))
                qty       = int(long_qty - short_qty)
                asset_type = inst.get("assetType", "EQUITY")
                # Options are priced per share; 1 contract = 100 shares.
                # averagePrice and the derived current_price must both be per-share
                # so that P&L math in the dashboard is consistent.
                multiplier = 100 if asset_type == "OPTION" else 1
                market_value = float(p.get("marketValue", 0.0))
                if qty != 0:
                    current_price = market_value / (qty * multiplier)
                else:
                    current_price = float(p.get("averagePrice", 0.0))
                # Schwab returns total open P&L under several possible keys.
                unrealized_pnl = next(
                    (float(p[k]) for k in (
                        "unrealizedPL", "longOpenProfitLoss",
                        "shortOpenProfitLoss", "currentDayProfitLoss",
                    ) if p.get(k) is not None),
                    0.0,
                )
                positions.append(Position(
                    ticker         = inst.get("symbol", "UNKNOWN"),
                    qty            = qty,
                    avg_cost       = float(p.get("averagePrice", 0.0)),
                    current_price  = current_price,
                    unrealized_pnl = unrealized_pnl,
                    strategy_name  = "unknown",
                    position_type  = _parse_position_type(inst, long_qty, short_qty),
                    opened_at      = datetime.now(tz=timezone.utc),
                ))
            logger.debug("get_positions returned %d positions", len(positions))
            return positions
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_positions failed: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_transactions(self, days: int = 30) -> list[Transaction]:
        """Return recent transactions for P&L reconciliation."""
        try:
            end   = datetime.now(tz=timezone.utc)
            start = end - timedelta(days=days)
            resp  = self._client.get_transactions(
                account_hash = self._account_hash,
                start_date   = start.strftime("%Y-%m-%d"),
                end_date     = end.strftime("%Y-%m-%d"),
            )
            resp.raise_for_status()
            txns = []
            for t in resp.json():
                item = t.get("transferItems", [{}])[0]
                txns.append(Transaction(
                    transaction_id = str(t.get("activityId", "")),
                    ticker         = item.get("instrument", {}).get("symbol", ""),
                    action         = t.get("type", ""),
                    qty            = abs(int(float(item.get("amount", 0)))),
                    price          = float(item.get("price", t.get("netAmount", 0.0))),
                    commission     = abs(float(t.get("fees", {}).get("commission", 0.0))),
                    executed_at    = (
                        datetime.fromisoformat(t["tradeDate"])
                        if "tradeDate" in t else datetime.now(tz=timezone.utc)
                    ),
                ))
            return txns
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_transactions failed: {exc}") from exc

    # ── Orders — Equity ───────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def place_order(self, order: Order) -> OrderResult:
        """
        Place a real equity order.
        Only reached when ENV=live + DRY_RUN=False + LIVE_TRADING_ENABLED=True.
        Triple-gate enforced by OrderRouter — not here.
        """
        try:
            order_spec = self._build_equity_order(order)
            resp = self._client.place_order(self._account_hash, order_spec)
            resp.raise_for_status()
            order_id = resp.headers.get("location", resp.headers.get("Location", "")).split("/")[-1]
            logger.warning(
                "LIVE ORDER PLACED | order_id=%s action=%s ticker=%s qty=%d strategy=%s",
                order_id, order.action, order.ticker, order.qty, order.strategy_name,
            )
            return OrderResult(
                order_id     = order_id,
                fill_price   = order.limit_price or 0.0,
                commission   = 0.0,
                filled_at    = datetime.now(tz=timezone.utc),
                status       = "submitted",
                raw_response = {"order_id": order_id},
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"place_order failed: {exc}", order=order) from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def preview_order(self, order: Order) -> OrderPreview:
        """
        Call Schwab previewOrder API — real validation, no real trade.
        Used when ENV=paper. Returns actual buying power impact from Schwab.
        """
        try:
            order_spec = self._build_equity_order(order)
            resp       = self._client.preview_order(self._account_hash, order_spec)
            resp.raise_for_status()
            data       = resp.json()
            # Schwab preview response shape (from API reference):
            #   orderStrategy.orderBalance.{orderValue, projectedCommission, projectedBuyingPower}
            #   orderValidationResult.rejects[].message  (non-empty = invalid)
            ostrat   = data.get("orderStrategy", {})
            balance  = ostrat.get("orderBalance", {})
            rejects  = data.get("orderValidationResult", {}).get("rejects", [])
            alerts   = data.get("orderValidationResult", {}).get("alerts", [])
            errors   = rejects or alerts
            is_valid = len(errors) == 0
            rejection = " | ".join(e.get("message", "") for e in errors) if errors else None
            logger.info(
                "Order preview | ticker=%s valid=%s rejection=%s",
                order.ticker, is_valid, rejection,
            )
            return OrderPreview(
                estimated_cost      = abs(float(balance.get("orderValue", 0.0))),
                buying_power_effect = float(balance.get("projectedBuyingPower", 0.0)),
                margin_impact       = 0.0,
                fees                = float(balance.get("projectedCommission", 0.0)),
                is_valid            = is_valid,
                rejection_reason    = rejection,
                raw_response        = data,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"preview_order failed: {exc}", order=order) from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by ID."""
        try:
            resp = self._client.cancel_order(
                account_hash = self._account_hash,
                order_id     = order_id,
            )
            resp.raise_for_status()
            logger.info("Order cancelled | order_id=%s", order_id)
            return True
        except Exception as exc:
            raise BrokerError(f"cancel_order failed for {order_id}: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_order_status(self, order_id: str) -> OrderStatus:
        """Poll order fill status."""
        try:
            resp = self._client.get_order(
                account_hash = self._account_hash,
                order_id     = order_id,
            )
            resp.raise_for_status()
            data = resp.json()
            status_map = {
                "WORKING"           : "pending",
                "PENDING_ACTIVATION": "pending",
                "QUEUED"            : "pending",
                "FILLED"            : "filled",
                "PARTIALLY_FILLED"  : "partial",
                "CANCELED"          : "cancelled",
                "REJECTED"          : "rejected",
                "EXPIRED"           : "cancelled",
            }
            raw_status = data.get("status", "UNKNOWN")
            logger.debug("Order status | order_id=%s status=%s", order_id, raw_status)
            return OrderStatus(
                order_id     = order_id,
                status       = status_map.get(raw_status, "unknown"),
                fill_price   = float(data.get("price", 0.0)),
                qty_filled   = int(float(data.get("filledQuantity", 0))),
                raw_response = data,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_order_status failed for {order_id}: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_order_history(self, days: int = 30) -> list[Order]:
        """Return recent order history."""
        try:
            end   = datetime.now(tz=timezone.utc)
            start = end - timedelta(days=days)
            resp  = self._client.get_orders_for_account(
                account_hash          = self._account_hash,
                from_entered_datetime = start,
                to_entered_datetime   = end,
            )
            resp.raise_for_status()
            orders = []
            for o in resp.json():
                leg = o.get("orderLegCollection", [{}])[0]
                orders.append(Order(
                    ticker        = leg.get("instrument", {}).get("symbol", ""),
                    action        = leg.get("instruction", ""),
                    qty           = int(float(o.get("quantity", 0))),
                    order_type    = o.get("orderType", "MARKET").lower(),
                    limit_price   = o.get("price"),
                    stop_price    = o.get("stopPrice"),
                    strategy_name = "unknown",
                    account_id    = str(settings.SCHWAB_ACCOUNT_NUMBER),
                ))
            return orders
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"get_order_history failed: {exc}") from exc

    # ── Orders — Options ──────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def place_options_order(self, order: OptionsOrder) -> OrderResult:
        """
        Place a real options order.
        ALWAYS requires limit_price — market orders for options are blocked.
        """
        try:
            order_spec = self._build_options_order(order)
            resp = self._client.place_order(self._account_hash, order_spec)
            resp.raise_for_status()
            order_id = resp.headers.get("location", resp.headers.get("Location", "")).split("/")[-1]
            logger.warning(
                "LIVE OPTIONS ORDER | order_id=%s action=%s contract=%s qty=%d",
                order_id, order.action, order.contract, order.qty,
            )
            return OrderResult(
                order_id     = order_id,
                fill_price   = order.limit_price,
                commission   = settings.PAPER_OPTIONS_COMMISSION * order.qty,
                filled_at    = datetime.now(tz=timezone.utc),
                status       = "submitted",
                raw_response = {"order_id": order_id},
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"place_options_order failed: {exc}", order=order) from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def preview_options_order(self, order: OptionsOrder) -> OrderPreview:
        """
        Call Schwab previewOrder API for options — real validation, not a stub.
        Used when ENV=paper for realistic options paper trading.
        """
        try:
            order_spec = self._build_options_order(order)
            resp       = self._client.preview_order(self._account_hash, order_spec)
            resp.raise_for_status()
            data     = resp.json()
            ostrat   = data.get("orderStrategy", {})
            balance  = ostrat.get("orderBalance", {})
            rejects  = data.get("orderValidationResult", {}).get("rejects", [])
            alerts   = data.get("orderValidationResult", {}).get("alerts", [])
            errors   = rejects or alerts
            is_valid = len(errors) == 0
            rejection = " | ".join(e.get("message", "") for e in errors) if errors else None
            logger.info(
                "Options preview | contract=%s valid=%s rejection=%s",
                order.contract, is_valid, rejection,
            )
            return OrderPreview(
                estimated_cost      = abs(float(balance.get("orderValue", 0.0))),
                buying_power_effect = float(balance.get("projectedBuyingPower", 0.0)),
                margin_impact       = 0.0,
                fees                = float(balance.get("projectedCommission", settings.PAPER_OPTIONS_COMMISSION * order.qty)),
                is_valid            = is_valid,
                rejection_reason    = rejection,
                raw_response        = data,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"preview_options_order failed: {exc}", order=order) from exc

    # ── Market data ───────────────────────────────────────────────────────────

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(DataError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_quote(self, ticker: str) -> Quote:
        """Real-time bid/ask/last. Supports equities, $SPX, $VIX, ETFs."""
        try:
            resp = self._client.get_quote(ticker)
            resp.raise_for_status()
            data = resp.json()
            if ticker not in data:
                raise DataError(f"Ticker {ticker} not found in Schwab quote response")
            q = data[ticker]["quote"]
            return Quote(
                ticker    = ticker,
                bid       = float(q.get("bidPrice", 0.0)),
                ask       = float(q.get("askPrice", 0.0)),
                last      = float(q.get("lastPrice", 0.0)),
                volume    = int(q.get("totalVolume", 0)),
                timestamp = datetime.now(tz=timezone.utc),
            )
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"get_quote failed for {ticker}: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(DataError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_options_chain(self, ticker: str, expiry: Optional[str] = None) -> OptionsChain:
        """Full options chain with Greeks. expiry: 'YYYY-MM-DD' or None for nearest."""
        try:
            kwargs: dict[str, Any] = dict(
                symbol                   = ticker,
                contract_type            = self._client.Options.ContractType.ALL,
                strike_count             = 20,
                include_underlying_quote = True,
            )
            if expiry:
                kwargs["from_date"] = expiry
                kwargs["to_date"]   = expiry

            resp = self._client.get_option_chain(**kwargs)
            resp.raise_for_status()
            data  = resp.json()
            calls = _parse_option_contracts(data.get("callExpDateMap", {}), "call")
            puts  = _parse_option_contracts(data.get("putExpDateMap",  {}), "put")
            logger.debug(
                "Options chain | ticker=%s calls=%d puts=%d",
                ticker, len(calls), len(puts),
            )
            return OptionsChain(
                ticker = ticker,
                expiry = expiry or "nearest",
                calls  = calls,
                puts   = puts,
            )
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"get_options_chain failed for {ticker}: {exc}") from exc

    @retry(max_attempts=settings.API_MAX_RETRIES, backoff_seconds=settings.API_BACKOFF_SECONDS, exceptions=(BrokerError,))
    @circuit_breaker(failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES, recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT)
    def get_market_hours(self) -> MarketHours:
        """Return whether the equity market is currently open."""
        try:
            resp = self._client.get_markets_hours(markets=["EQUITY"])
            resp.raise_for_status()
            data   = resp.json()
            # Handle both response shapes: equity.equity and equity.EQ
            equity = (
                data.get("equity", {}).get("equity")
                or data.get("equity", {}).get("EQ")
                or {}
            )
            is_open = bool(equity.get("isOpen", False))
            session = equity.get("sessionHours", {}).get("regularMarket", [{}])
            logger.debug("Market hours | is_open=%s", is_open)
            return MarketHours(
                is_open    = is_open,
                open_time  = session[0].get("start") if session else None,
                close_time = session[0].get("end")   if session else None,
            )
        except Exception as exc:
            raise BrokerError(f"get_market_hours failed: {exc}") from exc

    def stream_quotes(self, tickers: list[str], callback: Callable) -> None:
        """
        Stream real-time Level 1 quotes via Schwab WebSocket.
        Full streaming via schwab-py StreamClient is a V2 milestone.
        Polling fallback active until then — fires callback once per call.
        """
        logger.warning(
            "stream_quotes using polling fallback (streaming is a V2 milestone) | tickers=%s",
            tickers,
        )
        for ticker in tickers:
            try:
                quote = self.get_quote(ticker)
                callback(quote)
            except DataError as exc:
                logger.error("Polling fallback quote failed for %s: %s", ticker, exc)

    # ── Internal order builders ───────────────────────────────────────────────

    def _build_equity_order(self, order: Order):
        """Convert Order pydantic model to schwab-py order spec."""
        builder = self._schwab.orders.equities
        action  = order.action.upper()
        otype   = order.order_type.upper()

        if otype == "MARKET":
            return (
                builder.equity_buy_market(order.ticker, order.qty)
                if action == "BUY"
                else builder.equity_sell_market(order.ticker, order.qty)
            )
        elif otype == "LIMIT":
            if order.limit_price is None:
                raise BrokerError("LIMIT order requires limit_price", order=order)
            return (
                builder.equity_buy_limit(order.ticker, order.qty, order.limit_price)
                if action == "BUY"
                else builder.equity_sell_limit(order.ticker, order.qty, order.limit_price)
            )
        elif otype == "STOP":
            if order.stop_price is None:
                raise BrokerError("STOP order requires stop_price", order=order)
            common = self._schwab.orders.common
            ob = common.OrderBuilder()
            ob.set_order_type(common.OrderType.STOP)
            ob.set_stop_price(order.stop_price)
            ob.set_duration(common.Duration.DAY)
            ob.set_session(common.Session.NORMAL)
            instruction = (
                common.EquityInstruction.BUY if action == "BUY"
                else common.EquityInstruction.SELL
            )
            ob.add_equity_leg(instruction, order.ticker, order.qty)
            return ob
        elif otype == "STOP_LIMIT":
            if order.stop_price is None or order.limit_price is None:
                raise BrokerError(
                    "STOP_LIMIT order requires both stop_price and limit_price",
                    order=order,
                )
            common = self._schwab.orders.common
            ob = common.OrderBuilder()
            ob.set_order_type(common.OrderType.STOP_LIMIT)
            ob.set_stop_price(order.stop_price)
            ob.set_price(order.limit_price)
            ob.set_duration(common.Duration.DAY)
            ob.set_session(common.Session.NORMAL)
            instruction = (
                common.EquityInstruction.BUY if action == "BUY"
                else common.EquityInstruction.SELL
            )
            ob.add_equity_leg(instruction, order.ticker, order.qty)
            return ob
        else:
            raise BrokerError(
                f"Unsupported order_type: {order.order_type}. "
                "Supported: MARKET | LIMIT | STOP | STOP_LIMIT",
                order=order,
            )

    def _build_options_order(self, order: OptionsOrder):
        """
        Convert OptionsOrder pydantic model to schwab-py options order spec.
        ALWAYS limit — market orders for options are BLOCKED (too risky).
        """
        if order.limit_price is None:
            raise BrokerError(
                "Options orders MUST have limit_price. "
                "Market orders for options are blocked — use limit at mid-price.",
                order=order,
            )

        builder = self._schwab.orders.options
        action  = order.action.upper()

        action_map = {
            "BUY"          : builder.option_buy_to_open_limit,
            "BUY_TO_OPEN"  : builder.option_buy_to_open_limit,
            "SELL_TO_OPEN" : builder.option_sell_to_open_limit,
            "BUY_TO_CLOSE" : builder.option_buy_to_close_limit,
            "SELL"         : builder.option_sell_to_close_limit,
            "SELL_TO_CLOSE": builder.option_sell_to_close_limit,
        }

        build_fn = action_map.get(action)
        if build_fn is None:
            raise BrokerError(
                f"Unsupported options action: {order.action}. "
                "Supported: BUY | BUY_TO_OPEN | SELL_TO_OPEN | BUY_TO_CLOSE | SELL | SELL_TO_CLOSE",
                order=order,
            )

        return build_fn(order.contract, order.qty, order.limit_price)


__all__ = ["SchwabBroker"]