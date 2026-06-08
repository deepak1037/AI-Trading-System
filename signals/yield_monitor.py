"""10-year Treasury yield delta monitor (Day 2).

Establishes an 8 AM ET baseline each trading day and tracks the intraday
yield change. When the absolute delta exceeds YIELD_DELTA_THRESHOLD (in
percentage points / bps expressed as a decimal, e.g. 0.05 = 5 bps), a
Signal is emitted.

Data source: FRED series DGS10 for daily yield; yfinance TNX for intraday.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.exceptions import DataError, SignalError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from signals.signal_schema import Direction, Signal

logger = get_logger(__name__)

# FRED daily 10-yr constant-maturity yield
_FRED_SERIES = "DGS10"
# yfinance ticker for 10-yr Treasury yield (quoted as percentage, e.g. 4.25)
_YF_TICKER = "^TNX"


class YieldMonitor:
    """Monitors the 10-year Treasury yield intraday and fires signals on moves.

    Usage:
        monitor = YieldMonitor()
        monitor.set_baseline()           # call at 8 AM ET
        signal = monitor.check_delta()   # call periodically during the day
    """

    def __init__(self) -> None:
        self._baseline: Optional[float] = None
        self._baseline_time: Optional[datetime] = None

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
    )
    def _fetch_current_yield(self) -> float:
        """Fetch the current 10-yr yield via yfinance (in percent, e.g. 4.25)."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            data = yf.Ticker(_YF_TICKER).history(period="1d", interval="1m")
            if data.empty:
                raise DataError("No intraday yield data from yfinance")
            return float(data["Close"].iloc[-1])
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"yfinance yield fetch failed: {exc}") from exc

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    def _fetch_fred_daily_yield(self) -> float:
        """Fetch the latest daily DGS10 from FRED."""
        if not settings.FRED_API_KEY:
            # TODO: FRED_API_KEY not configured — add to .env
            raise DataError("FRED_API_KEY not set in config")
        try:
            from fredapi import Fred  # type: ignore[import-untyped]

            fred = Fred(api_key=settings.FRED_API_KEY)
            data = fred.get_series(_FRED_SERIES, limit=5).dropna()
            if data.empty:
                raise DataError("No DGS10 data from FRED")
            return float(data.iloc[-1])
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"FRED yield fetch failed: {exc}") from exc

    def set_baseline(self, yield_pct: Optional[float] = None) -> float:
        """Record the 8 AM baseline yield.

        Args:
            yield_pct: Optional override (useful in tests / backtest replay).
                       If None, fetches live from FRED.

        Returns:
            The baseline yield that was recorded.
        """
        if yield_pct is not None:
            self._baseline = yield_pct
        else:
            try:
                self._baseline = self._fetch_current_yield()
            except DataError:
                logger.warning("Intraday yield unavailable; falling back to FRED daily")
                self._baseline = self._fetch_fred_daily_yield()

        self._baseline_time = datetime.now(tz=timezone.utc)
        logger.info(
            "YieldMonitor: baseline set at %.4f%% at %s",
            self._baseline,
            self._baseline_time.isoformat(),
        )
        return self._baseline

    def get_delta(self, current_yield: Optional[float] = None) -> float:
        """Return current yield − baseline (both in percent points).

        A positive delta means yields rose (bearish for equities).
        A negative delta means yields fell (bullish for equities).
        """
        if self._baseline is None:
            raise SignalError("Baseline not set; call set_baseline() first")

        if current_yield is None:
            current_yield = self._fetch_current_yield()

        return current_yield - self._baseline

    def _signal_from_delta(self, delta: float) -> Signal:
        """Map a yield delta to a directional Signal.

        Positive delta (rising yields) → bearish (short / strong_short).
        Negative delta (falling yields) → bullish (long / strong_long).
        Below ``YIELD_DELTA_THRESHOLD`` → neutral with confidence scaling toward
        the threshold (so it's still a usable continuous reading).
        """
        abs_delta = abs(delta)
        thr = settings.YIELD_DELTA_THRESHOLD

        if abs_delta < thr:
            direction: Direction = "neutral"
            confidence = int(abs_delta / thr * 50) if thr > 0 else 0
        else:
            # 5 bps → confidence 55, every additional 5 bps adds ~10 pts, cap 95
            confidence = min(95, 55 + int((abs_delta - thr) / 0.05 * 10))
            if abs_delta >= thr * 4:
                direction = "strong_short" if delta > 0 else "strong_long"
            else:
                direction = "short" if delta > 0 else "long"

        baseline = self._baseline if self._baseline is not None else 0.0
        current = baseline + delta
        return Signal(
            direction=direction,
            confidence=confidence,
            source="yield",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "yield_current": round(current, 4),
                "yield_baseline": round(baseline, 4),
                "yield_delta": round(delta, 4),
                "threshold": thr,
            },
        )

    def check_delta(self, current_yield: Optional[float] = None) -> Optional[Signal]:
        """Return a Signal only if the yield delta exceeds threshold (alerts).

        Use ``read()`` for a continuous reading in the fusion tick.
        """
        delta = self.get_delta(current_yield)
        if abs(delta) < settings.YIELD_DELTA_THRESHOLD:
            logger.debug(
                "YieldMonitor: delta=%.4f below threshold=%.4f — no signal",
                delta, settings.YIELD_DELTA_THRESHOLD,
            )
            return None
        signal = self._signal_from_delta(delta)
        logger.info(
            "YieldMonitor: delta=%.4f%% → direction=%s confidence=%d",
            delta, signal.direction, signal.confidence,
        )
        return signal

    def read(self) -> Optional[Signal]:
        """Continuous yield reading for the fusion tick — always a Signal.

        Auto-sets the baseline on first call (the cause of yield never appearing:
        ``check_delta()`` raises if no baseline). Below-threshold moves return a
        neutral reading so yield contributes every tick alongside technical/
        sentiment. Returns None only if the yield data can't be fetched at all.
        """
        try:
            if self._baseline is None:
                self.set_baseline()  # intraday yfinance (FRED daily fallback)
            delta = self.get_delta()
        except (SignalError, DataError) as exc:
            logger.debug("YieldMonitor.read: %s", exc)
            return None
        signal = self._signal_from_delta(delta)
        logger.debug(
            "YieldMonitor.read: delta=%.4f dir=%s conf=%d",
            delta, signal.direction, signal.confidence,
        )
        return signal

    @property
    def baseline(self) -> Optional[float]:
        return self._baseline


__all__ = ["YieldMonitor"]
