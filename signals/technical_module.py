"""Technical indicators module using pandas-ta (Day 4).

Computes RSI, MACD, Bollinger Bands, and moving average alignment from
OHLCV data, then produces a Signal. All parameters are config-driven.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.exceptions import DataError, SignalError
from core.logger import get_logger
from signals.signal_schema import Direction, Signal

logger = get_logger(__name__)

# Default indicator params (could be added to settings later)
_RSI_PERIOD = 14
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_BB_PERIOD = 20
_BB_STD = 2.0
_MA_SHORT = 20
_MA_MID = 50
_MA_LONG = 200


class TechnicalModule:
    """Computes technical signals from OHLCV price data.

    Accepts a pandas DataFrame with columns: open, high, low, close, volume.
    All column names should be lowercase.
    """

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Append RSI, MACD, BB, and MA columns. Returns enriched DataFrame."""
        try:
            import pandas_ta as ta  # type: ignore[import-untyped]  # noqa: F401
        except ImportError as exc:
            raise DataError("pandas-ta not installed") from exc

        df = df.copy()

        # RSI
        df.ta.rsi(length=_RSI_PERIOD, append=True)

        # MACD
        df.ta.macd(
            fast=_MACD_FAST,
            slow=_MACD_SLOW,
            signal=_MACD_SIGNAL,
            append=True,
        )

        # Bollinger Bands
        df.ta.bbands(length=_BB_PERIOD, std=_BB_STD, append=True)

        # Moving averages
        df.ta.sma(length=_MA_SHORT, append=True)
        df.ta.sma(length=_MA_MID, append=True)
        df.ta.sma(length=_MA_LONG, append=True)

        return df

    def _rsi_signal(self, rsi: float) -> tuple[Optional[Direction], int]:
        """RSI-based direction and partial confidence."""
        if rsi >= 70:
            return "short", 60  # overbought → bearish
        elif rsi <= 30:
            return "long", 60   # oversold → bullish
        elif rsi >= 60:
            return "short", 35
        elif rsi <= 40:
            return "long", 35
        return "neutral", 20

    def _macd_signal(self, macd: float, macd_signal: float, hist: float) -> tuple[Optional[Direction], int]:
        """MACD crossover + histogram direction."""
        if macd > macd_signal and hist > 0:
            conf = min(70, 40 + int(abs(hist) * 100))
            return "long", conf
        elif macd < macd_signal and hist < 0:
            conf = min(70, 40 + int(abs(hist) * 100))
            return "short", conf
        return "neutral", 20

    def _bb_signal(self, close: float, upper: float, lower: float, mid: float) -> tuple[Optional[Direction], int]:
        """Bollinger Band position signal."""
        band_width = upper - lower
        if band_width == 0:
            return "neutral", 15
        pct_b = (close - lower) / band_width

        if pct_b >= 1.0:
            return "short", 55  # price above upper band → overbought
        elif pct_b <= 0.0:
            return "long", 55   # price below lower band → oversold
        elif pct_b >= 0.8:
            return "short", 30
        elif pct_b <= 0.2:
            return "long", 30
        return "neutral", 15

    def _ma_alignment(self, close: float, ma20: float, ma50: float, ma200: float) -> tuple[Direction, int]:
        """Check timeframe alignment of MAs. All bullish or all bearish."""
        if close > ma20 > ma50 > ma200:
            return "long", 70   # full bullish stack
        elif close < ma20 < ma50 < ma200:
            return "short", 70  # full bearish stack
        elif close > ma20 > ma50:
            return "long", 45
        elif close < ma20 < ma50:
            return "short", 45
        return "neutral", 20

    def score(self, df: pd.DataFrame, ticker: str = "UNKNOWN") -> Signal:
        """Produce a Signal from OHLCV DataFrame.

        Args:
            df: DataFrame with columns [open, high, low, close, volume],
                indexed by datetime. Must have at least 200 rows for full MA.
            ticker: Symbol label for logging.

        Returns:
            Signal with source="technical".
        """
        if not isinstance(df, pd.DataFrame):
            raise SignalError(
                f"score() expects an OHLCV DataFrame, got {type(df).__name__}. "
                "To score by symbol, use score_ticker('SPY').",
                ticker=ticker if isinstance(ticker, str) else "UNKNOWN",
            )
        if len(df) < 30:
            raise SignalError(
                f"Insufficient data for technical analysis: {len(df)} rows (need ≥30)",
                ticker=ticker,
            )

        required_cols = {"open", "high", "low", "close", "volume"}
        missing = required_cols - set(df.columns.str.lower())
        if missing:
            raise SignalError(f"Missing columns: {missing}", ticker=ticker)

        # Normalise column names
        df = df.copy()
        df.columns = df.columns.str.lower()

        enriched = self._compute_indicators(df)
        last = enriched.iloc[-1]

        # RSI
        rsi_col = f"RSI_{_RSI_PERIOD}"
        rsi_dir, rsi_conf = self._rsi_signal(float(last.get(rsi_col, 50)))

        # MACD
        macd_col = f"MACD_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
        signal_col = f"MACDs_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
        hist_col = f"MACDh_{_MACD_FAST}_{_MACD_SLOW}_{_MACD_SIGNAL}"
        macd_val = float(last.get(macd_col, 0) or 0)
        sig_val = float(last.get(signal_col, 0) or 0)
        hist_val = float(last.get(hist_col, 0) or 0)
        macd_dir, macd_conf = self._macd_signal(macd_val, sig_val, hist_val)

        # Bollinger Bands
        upper_col = f"BBU_{_BB_PERIOD}_{_BB_STD}"
        lower_col = f"BBL_{_BB_PERIOD}_{_BB_STD}"
        mid_col = f"BBM_{_BB_PERIOD}_{_BB_STD}"
        upper = float(last.get(upper_col, last["close"]) or last["close"])
        lower_bb = float(last.get(lower_col, last["close"]) or last["close"])
        mid_bb = float(last.get(mid_col, last["close"]) or last["close"])
        bb_dir, bb_conf = self._bb_signal(float(last["close"]), upper, lower_bb, mid_bb)

        # MA alignment
        ma20 = float(last.get(f"SMA_{_MA_SHORT}", last["close"]) or last["close"])
        ma50 = float(last.get(f"SMA_{_MA_MID}", last["close"]) or last["close"])
        ma200 = float(last.get(f"SMA_{_MA_LONG}", last["close"]) or last["close"])
        ma_dir, ma_conf = self._ma_alignment(float(last["close"]), ma20, ma50, ma200)

        # Weighted vote (RSI 25%, MACD 30%, BB 20%, MA 25%)
        score_map: dict[str, float] = {"strong_long": 2, "long": 1, "neutral": 0, "short": -1, "strong_short": -2}
        weights = [(rsi_dir, rsi_conf, 0.25), (macd_dir, macd_conf, 0.30),
                   (bb_dir, bb_conf, 0.20), (ma_dir, ma_conf, 0.25)]
        composite = sum(score_map.get(d or "neutral", 0) * w for d, _, w in weights)
        avg_conf = sum(c * w for _, c, w in weights)

        if composite >= 1.5:
            direction: Direction = "strong_long"
        elif composite >= 0.5:
            direction = "long"
        elif composite <= -1.5:
            direction = "strong_short"
        elif composite <= -0.5:
            direction = "short"
        else:
            direction = "neutral"

        confidence = min(95, int(avg_conf))

        logger.debug(
            "TechnicalModule[%s]: RSI=%.1f MACD_hist=%.4f BB_pctB=%.2f composite=%.2f "
            "direction=%s confidence=%d",
            ticker, float(last.get(rsi_col, 50) or 50), hist_val,
            (float(last["close"]) - lower_bb) / (upper - lower_bb + 1e-9),
            composite, direction, confidence,
        )

        return Signal(
            direction=direction,
            confidence=confidence,
            source="technical",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "ticker": ticker,
                "rsi": round(float(last.get(rsi_col, 50) or 50), 2),
                "macd_hist": round(hist_val, 6),
                "bb_upper": round(upper, 4),
                "bb_lower": round(lower_bb, 4),
                "ma20": round(ma20, 4),
                "ma50": round(ma50, 4),
                "ma200": round(ma200, 4),
                "composite_score": round(composite, 3),
            },
        )

    def score_ticker(self, ticker: str, period: str = "1y") -> Signal:
        """Fetch OHLCV for ``ticker`` via yfinance, then score it.

        Convenience wrapper around ``score(df)`` for when you have a symbol
        rather than a DataFrame::

            TechnicalModule().score_ticker("SPY")

        Args:
            ticker: Symbol, e.g. "SPY".
            period: yfinance history period (default "1y" — enough for the
                200-day MA).
        """
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - import guard
            raise DataError("yfinance not installed") from exc

        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        if df is None or df.empty:
            raise SignalError(f"No OHLCV data returned for {ticker}", ticker=ticker)
        df.columns = [c.lower() for c in df.columns]
        return self.score(df, ticker=ticker)


__all__ = ["TechnicalModule"]
