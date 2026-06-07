"""Paper trading fill simulator (Day 7).

Supports three fill methods (from settings.PAPER_FILL_METHOD):
  - next_open:  Fill at next-bar open (default for EOD signals)
  - vwap:       Fill at VWAP of bar (most accurate intraday)
  - worst_case: Fill at worst side of bar (conservative stress test)

Slippage and commission from settings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from broker_core.base_broker import Order, OptionsOrder, OrderResult
from paper_trading.paper_account import PaperAccount

logger = get_logger(__name__)


class PaperTradingEngine:
    """Simulates order fills for paper and backtest modes."""

    def __init__(self, account: Optional[PaperAccount] = None) -> None:
        self._account = account

    def _apply_slippage(self, price: float, action: str, is_options: bool = False) -> float:
        """Apply slippage to a fill price."""
        slippage_pct = (
            settings.PAPER_OPTIONS_SLIPPAGE if is_options
            else settings.PAPER_SLIPPAGE_PCT
        )
        if action in ("BUY", "BUY_TO_OPEN", "BUY_TO_CLOSE"):
            return price * (1 + slippage_pct)
        else:
            return price * (1 - slippage_pct)

    def _fetch_ohlcv(self, ticker: str) -> dict:
        """Fetch latest OHLCV bar for fill calculation. Falls back to last close."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            data = yf.Ticker(ticker).history(period="2d", interval="1d")
            if data.empty:
                raise DataError(f"No OHLCV data for {ticker}")
            last = data.iloc[-1]
            vwap = float((last["High"] + last["Low"] + last["Close"]) / 3)
            return {
                "open": float(last["Open"]),
                "high": float(last["High"]),
                "low": float(last["Low"]),
                "close": float(last["Close"]),
                "volume": int(last["Volume"]),
                "vwap": vwap,
            }
        except Exception as exc:
            raise DataError(f"OHLCV fetch failed for {ticker}: {exc}") from exc

    def simulate_fill(
        self,
        order: Order,
        price_data: Optional[dict] = None,
    ) -> OrderResult:
        """Simulate an equity order fill.

        Args:
            order: The order to fill.
            price_data: Optional dict with {open, high, low, close, vwap}.
                        If None, fetches from yfinance.

        Returns:
            OrderResult with simulated fill_price and commission.
        """
        if price_data is None:
            try:
                price_data = self._fetch_ohlcv(order.ticker)
            except DataError as exc:
                logger.warning("Cannot fetch OHLCV for %s: %s — using 0.0", order.ticker, exc)
                price_data = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "vwap": 0.0}

        fill_method = settings.PAPER_FILL_METHOD
        raw_price: float

        if fill_method == "next_open":
            raw_price = price_data.get("open", price_data.get("close", 0.0))
        elif fill_method == "vwap":
            raw_price = price_data.get("vwap", price_data.get("close", 0.0))
        elif fill_method == "worst_case":
            if order.action in ("BUY", "BUY_TO_OPEN"):
                raw_price = price_data.get("high", price_data.get("close", 0.0))
            else:
                raw_price = price_data.get("low", price_data.get("close", 0.0))
        else:
            raw_price = price_data.get("close", 0.0)

        fill_price = self._apply_slippage(raw_price, order.action, is_options=False)
        commission = settings.PAPER_COMMISSION_PER_SHARE * order.qty

        if self._account is not None:
            self._account.apply_fill(order, fill_price, commission)

        logger.debug(
            "PaperEngine.simulate_fill: %s %s x%d fill=%.4f method=%s",
            order.action, order.ticker, order.qty, fill_price, fill_method,
        )

        return OrderResult(
            order_id=f"PAPER-{order.ticker}-{order.qty}-{int(datetime.now(tz=timezone.utc).timestamp())}",
            fill_price=fill_price,
            commission=commission,
            status="filled",
            filled_at=datetime.now(tz=timezone.utc),
        )

    def simulate_options_fill(
        self,
        order: OptionsOrder,
        mid_price: Optional[float] = None,
    ) -> OrderResult:
        """Simulate an options order fill at mid-price ± slippage.

        Args:
            order: The options order to fill.
            mid_price: Mid-price of the contract. If None, uses limit_price.

        Returns:
            OrderResult with fill_price and commission.
        """
        raw_price = mid_price or order.limit_price or 0.0
        fill_price = self._apply_slippage(raw_price, order.action, is_options=True)
        commission = settings.PAPER_OPTIONS_COMMISSION * order.qty

        logger.debug(
            "PaperEngine.simulate_options_fill: %s %s x%d fill=%.4f",
            order.action, order.contract, order.qty, fill_price,
        )

        return OrderResult(
            order_id=f"PAPER-OPT-{order.contract}-{order.qty}",
            fill_price=fill_price,
            commission=commission,
            status="filled",
            filled_at=datetime.now(tz=timezone.utc),
        )


__all__ = ["PaperTradingEngine"]
