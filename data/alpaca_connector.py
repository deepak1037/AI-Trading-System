"""Alpaca connector — real-time quotes and historical bars via alpaca-py.

Uses ``StockHistoricalDataClient`` (data.alpaca.markets, no auth required for
free tier) for bars, and requires API keys for the latest-quote endpoint.
All external calls are wrapped with ``@retry`` + ``@circuit_breaker``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from data.rate_limiter import RateLimiter

logger = get_logger(__name__)


class AlpacaConnector:
    """Real-time and historical equity data via Alpaca Data API v2.

    Args:
        rate_limiter: Injectable for tests; defaults to ``settings.ALPACA_RATE_LIMIT``.
    """

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self._limiter = rate_limiter or RateLimiter(settings.ALPACA_RATE_LIMIT)
        # Client is created lazily so that tests can instantiate without credentials.
        self._client: StockHistoricalDataClient | None = None

        self._get_quote_impl = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_quote)
        )

        self._get_bars_impl = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_bars)
        )

    # ── Public API ────────────────────────────────────────────

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """Return the latest NBBO quote for ``ticker``.

        Returns:
            Dict with keys ``ticker``, ``bid``, ``ask``, ``bid_size``,
            ``ask_size``, ``timestamp``.

        Raises:
            DataError: If the API key is missing or the call fails.
        """
        if not settings.ALPACA_API_KEY:
            raise DataError(
                "ALPACA_API_KEY not configured",
                hint="Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env",
            )
        return self._get_quote_impl(ticker)

    def get_bars(
        self,
        ticker: str,
        start: str,
        end: str,
        timeframe: TimeFrame = TimeFrame.Day,
    ) -> pd.DataFrame:
        """Return OHLCV bars as a DataFrame indexed by datetime.

        Args:
            ticker: Stock symbol.
            start:  ISO-8601 date/datetime string.
            end:    ISO-8601 date/datetime string.
            timeframe: ``TimeFrame.Day``, ``TimeFrame.Hour``, etc.

        Raises:
            DataError: If the API call fails after retries.
        """
        return self._get_bars_impl(ticker, start, end, timeframe)

    # ── Private: API calls ────────────────────────────────────

    def _client_or_raise(self) -> StockHistoricalDataClient:
        if self._client is None:
            self._client = StockHistoricalDataClient(
                api_key=settings.ALPACA_API_KEY or None,
                secret_key=settings.ALPACA_SECRET_KEY or None,
            )
        return self._client

    def _fetch_quote(self, ticker: str) -> dict[str, Any]:
        self._limiter.acquire()
        try:
            client = self._client_or_raise()
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            result = client.get_stock_latest_quote(req)
            quote = result[ticker]
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"Alpaca latest quote failed for {ticker}",
                ticker=ticker,
                cause=str(exc),
            ) from exc

        return {
            "ticker": ticker,
            "bid": float(quote.bid_price),
            "ask": float(quote.ask_price),
            "bid_size": int(quote.bid_size),
            "ask_size": int(quote.ask_size),
            "timestamp": quote.timestamp,
        }

    def _fetch_bars(
        self,
        ticker: str,
        start: str,
        end: str,
        timeframe: TimeFrame,
    ) -> pd.DataFrame:
        self._limiter.acquire()
        try:
            client = self._client_or_raise()
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=timeframe,
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
            )
            bars = client.get_stock_bars(req)
            df = cast(pd.DataFrame, bars.df)  # type: ignore[union-attr]
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"Alpaca bars failed for {ticker}",
                ticker=ticker,
                start=start,
                end=end,
                cause=str(exc),
            ) from exc

        if df.empty:
            logger.warning(
                "Alpaca returned empty bars for %s %s→%s", ticker, start, end
            )
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        # alpaca-py returns multi-index (symbol, timestamp); drop symbol level.
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        logger.debug("Alpaca bars %s: %d rows", ticker, len(df))
        return df


__all__ = ["AlpacaConnector"]
