"""FRED connector — macro time series from the St. Louis Fed.

Uses the ``fredapi`` library.  API key is optional for low-volume use but
required for production; set ``FRED_API_KEY`` in .env.  All calls go through
``@retry`` + ``@circuit_breaker``.
"""

from __future__ import annotations

import pandas as pd
from fredapi import Fred

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from data.rate_limiter import RateLimiter

logger = get_logger(__name__)


class FredConnector:
    """Macro time series from FRED (Federal Reserve Economic Data).

    Args:
        rate_limiter: Injectable for tests; defaults to ``settings.FRED_RATE_LIMIT``.
    """

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self._limiter = rate_limiter or RateLimiter(settings.FRED_RATE_LIMIT)
        # Lazily initialise the Fred client.
        self._fred: Fred | None = None

        self._series_impl = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_series)
        )

    # ── Public API ────────────────────────────────────────────

    def get_series(
        self,
        series_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.Series:
        """Return a FRED time series as a pandas Series with a DatetimeIndex.

        Args:
            series_id: FRED series ID, e.g. ``"T10YIE"``, ``"UNRATE"``.
            start:     ISO-8601 start date string, e.g. ``"2020-01-01"``.
            end:       ISO-8601 end date string.

        Raises:
            DataError: If the API call fails after all retries.
        """
        logger.debug("FRED get_series %s %s→%s", series_id, start, end)
        return self._series_impl(series_id, start, end)

    def get_latest(self, series_id: str) -> float:
        """Return the most recent observation value.

        Raises:
            DataError: If the series is empty or the call fails.
        """
        s = self.get_series(series_id)
        if s.empty:
            raise DataError("FRED series is empty", series_id=series_id)
        return float(s.dropna().iloc[-1])

    # ── Private ───────────────────────────────────────────────

    def _get_client(self) -> Fred:
        if self._fred is None:
            api_key = settings.FRED_API_KEY or None
            if not api_key:
                logger.warning("FRED_API_KEY not set — requests may be rate-limited")
            self._fred = Fred(api_key=api_key)
        return self._fred

    def _fetch_series(
        self,
        series_id: str,
        start: str | None,
        end: str | None,
    ) -> pd.Series:
        self._limiter.acquire()
        kwargs: dict = {}
        if start:
            kwargs["observation_start"] = start
        if end:
            kwargs["observation_end"] = end
        try:
            s = self._get_client().get_series(series_id, **kwargs)
        except Exception as exc:
            raise DataError(
                f"FRED series fetch failed: {series_id}",
                series_id=series_id,
                start=start,
                end=end,
                cause=str(exc),
            ) from exc

        logger.debug("FRED %s: %d observations", series_id, len(s))
        return s


__all__ = ["FredConnector"]
