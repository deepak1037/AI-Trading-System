"""yfinance connector — OHLCV data with transparent SQLite caching.

Cache strategy: if the cache contains rows for the full requested date range,
return cached data directly (no network call).  If the cache is empty for that
range, fetch from yfinance, upsert to cache, and return.

Always use the ``YFinanceConnector`` class — never call ``yfinance`` directly
from other modules.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from data.db import get_connection
from data.rate_limiter import RateLimiter

logger = get_logger(__name__)


class YFinanceConnector:
    """Fetch OHLCV bars via yfinance with transparent SQLite caching.

    Args:
        db_path: SQLite path; falls back to ``settings.DB_PATH``.
        rate_limiter: Injectable for tests; defaults to ``settings.YFINANCE_RATE_LIMIT``.
    """

    def __init__(
        self,
        db_path: str | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._db = db_path
        self._limiter = rate_limiter or RateLimiter(settings.YFINANCE_RATE_LIMIT)

        # Build retry + circuit_breaker wrappers bound to this instance's limiter
        # so that test instances don't share circuit state.
        self._fetch_with_retry = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_raw)
        )

    # ── Public API ────────────────────────────────────────────

    def get_ohlcv(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame for ``ticker`` in [start, end] (inclusive).

        Args:
            ticker: Stock symbol, e.g. ``"AAPL"``.
            start:  ISO-8601 date string, e.g. ``"2024-01-01"``.
            end:    ISO-8601 date string, e.g. ``"2024-12-31"``.
            interval: yfinance interval string (``"1d"``, ``"1h"``, etc.).

        Returns:
            DataFrame indexed by ``datetime.date``, columns lower-cased:
            ``open``, ``high``, ``low``, ``close``, ``volume``.

        Raises:
            DataError: If the API call fails after all retries.
        """
        if interval == "1d":
            cached = self._get_cached(ticker, start, end)
            if cached is not None and not cached.empty:
                logger.debug(
                    "ohlcv cache HIT  %s %s→%s (%d rows)",
                    ticker,
                    start,
                    end,
                    len(cached),
                )
                return cached

        fresh = self._fetch_with_retry(ticker, start, end, interval)

        if interval == "1d" and not fresh.empty:
            self._cache_ohlcv(ticker, fresh)

        return fresh

    def get_latest_close(self, ticker: str) -> float:
        """Return the most recent closing price from cache or a 5-day fetch.

        Raises:
            DataError: If no data can be obtained.
        """
        df = self.get_ohlcv(ticker, start="2000-01-01", end="2100-01-01")
        if df.empty:
            raise DataError("No close data available", ticker=ticker)
        return float(df["close"].iloc[-1])

    # ── Private: API call ─────────────────────────────────────

    def _fetch_raw(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Call yfinance.  Raises DataError on any failure."""
        self._limiter.acquire()
        try:
            t = yf.Ticker(ticker)
            df = t.history(start=start, end=end, interval=interval, auto_adjust=True)
        except Exception as exc:
            raise DataError(
                f"yfinance API error for {ticker}",
                ticker=ticker,
                interval=interval,
                cause=str(exc),
            ) from exc

        if df.empty:
            logger.warning(
                "yfinance returned empty DataFrame for %s %s→%s", ticker, start, end
            )
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        return self._normalise(df)

    # ── Private: cache helpers ────────────────────────────────

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        """Lower-case column names and normalise the date index."""
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]]
        # Strip timezone; convert DatetimeIndex to plain date objects.
        if hasattr(df.index, "tz_localize"):
            try:
                df.index = df.index.tz_localize(None)
            except TypeError:
                df.index = df.index.tz_convert(None)
        df.index = pd.to_datetime(df.index).date
        return df

    def _get_cached(self, ticker: str, start: str, end: str) -> pd.DataFrame | None:
        """Query ohlcv_cache; returns None when no rows exist for this range."""
        with get_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM ohlcv_cache "
                "WHERE ticker = ? AND date >= ? AND date <= ? "
                "ORDER BY date",
                (ticker, start, end),
            ).fetchall()
        if not rows:
            return None
        df = pd.DataFrame(
            [dict(r) for r in rows],
            columns=["date", "open", "high", "low", "close", "volume"],
        )
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.set_index("date")

    def _cache_ohlcv(self, ticker: str, df: pd.DataFrame) -> None:
        """Upsert OHLCV rows into ohlcv_cache (INSERT OR REPLACE)."""
        rows = [
            (
                ticker,
                str(idx),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row["volume"]),
            )
            for idx, row in df.iterrows()
        ]
        with get_connection(self._db) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ohlcv_cache "
                "(ticker, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        logger.debug("ohlcv cache WRITE %s — %d rows", ticker, len(rows))


__all__ = ["YFinanceConnector"]
