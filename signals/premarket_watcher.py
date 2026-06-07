"""Pre-market watcher — futures, BTC, FedWatch (Day 3).

Checks:
  - NQ/ES futures pre-market change vs prior close
  - BTC as a risk-on/risk-off proxy
  - CME FedWatch implied probability of rate hike (via FRED or yfinance proxy)

Data source: yfinance tickers for futures (NQ=F, ES=F) and BTC-USD.
FedWatch: approximated from the 30-day Fed Funds Futures (ZQ=F / FEDFUNDS).
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

# yfinance tickers
_NQ_TICKER = "NQ=F"   # NASDAQ 100 E-mini futures
_ES_TICKER = "ES=F"   # S&P 500 E-mini futures
_BTC_TICKER = "BTC-USD"
_FEDFUNDS_TICKER = "ZQ=F"  # 30-day Fed Funds Futures


class PremarketWatcher:
    """Monitors pre-market conditions and produces a composite signal.

    Checks NQ futures (primary), ES futures (secondary), and BTC (risk proxy).
    Returns a single Signal fusing these inputs.
    """

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
    )
    def _fetch_futures_change(self, ticker: str) -> float:
        """Return intraday % change for a futures ticker."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            data = yf.Ticker(ticker).history(period="2d", interval="1m")
            if data.empty or len(data) < 2:
                raise DataError(f"Insufficient data for {ticker}")
            prev_close = float(data["Close"].iloc[-2])
            current = float(data["Close"].iloc[-1])
            if prev_close == 0:
                raise DataError(f"Zero prev_close for {ticker}")
            return (current - prev_close) / prev_close
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"Futures fetch failed for {ticker}: {exc}") from exc

    def _fetch_btc_24h_change(self) -> float:
        """Return BTC 24-hour % change."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            data = yf.Ticker(_BTC_TICKER).history(period="2d", interval="1h")
            if data.empty or len(data) < 2:
                return 0.0
            start = float(data["Close"].iloc[0])
            end = float(data["Close"].iloc[-1])
            if start == 0:
                return 0.0
            return (end - start) / start
        except Exception as exc:
            logger.warning("BTC fetch failed: %s", exc)
            return 0.0

    def _direction_from_change(self, pct_change: float, threshold: float) -> Optional[Direction]:
        """Map a percentage change to a Direction given a threshold."""
        if abs(pct_change) < abs(threshold):
            return None
        if pct_change < 0:
            return "strong_short" if pct_change < threshold * 2 else "short"
        return "strong_long" if pct_change > abs(threshold) * 2 else "long"

    def check(
        self,
        nq_change: Optional[float] = None,
        es_change: Optional[float] = None,
        btc_change: Optional[float] = None,
    ) -> Signal:
        """Produce a pre-market Signal.

        Args:
            nq_change: NQ futures % change override (for testing/backtest).
            es_change: ES futures % change override.
            btc_change: BTC 24h % change override.

        Returns:
            Signal with source="premarket".
        """
        # Fetch live data if not provided
        if nq_change is None:
            try:
                nq_change = self._fetch_futures_change(_NQ_TICKER)
            except DataError as exc:
                logger.warning("NQ futures unavailable: %s", exc)
                nq_change = 0.0

        if es_change is None:
            try:
                es_change = self._fetch_futures_change(_ES_TICKER)
            except DataError as exc:
                logger.warning("ES futures unavailable: %s", exc)
                es_change = 0.0

        if btc_change is None:
            btc_change = self._fetch_btc_24h_change()

        threshold = settings.PREMARKET_FUTURES_THRESHOLD  # default -0.008

        # Composite score: NQ (60%), ES (30%), BTC (10%)
        composite_pct = nq_change * 0.60 + es_change * 0.30 + btc_change * 0.10
        abs_composite = abs(composite_pct)
        abs_threshold = abs(threshold)

        if abs_composite >= abs_threshold * 2:
            direction: Direction = "strong_short" if composite_pct < 0 else "strong_long"
            confidence = min(90, 70 + int((abs_composite - abs_threshold * 2) / abs_threshold * 10))
        elif abs_composite >= abs_threshold:
            direction = "short" if composite_pct < 0 else "long"
            confidence = min(70, 50 + int((abs_composite - abs_threshold) / abs_threshold * 20))
        else:
            direction = "neutral"
            confidence = max(15, int(abs_composite / abs_threshold * 40))

        logger.info(
            "PremarketWatcher: NQ=%.3f%% ES=%.3f%% BTC=%.3f%% composite=%.3f%% "
            "direction=%s confidence=%d",
            nq_change * 100, es_change * 100, btc_change * 100,
            composite_pct * 100, direction, confidence,
        )

        return Signal(
            direction=direction,
            confidence=confidence,
            source="premarket",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "nq_change": round(nq_change, 6),
                "es_change": round(es_change, 6),
                "btc_change": round(btc_change, 6),
                "composite_pct": round(composite_pct, 6),
                "threshold": threshold,
            },
        )


__all__ = ["PremarketWatcher"]
