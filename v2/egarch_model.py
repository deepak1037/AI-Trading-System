"""V2: Volatility magnitude forecaster using EGARCH (Days 19-25).

Uses the `arch` library (MIT) to fit EGARCH(1,1) and forecast 1-5 day
expected volatility. This informs magnitude estimation for V2 signals:
"market will move -2% to -4.5%" rather than just "direction=short".

CLAUDE.md Section 19: Output RANGE not point estimate — honest about uncertainty.
"""

from __future__ import annotations


import numpy as np
import pandas as pd

from core.exceptions import SignalError
from core.logger import get_logger

logger = get_logger(__name__)


class EGARCHForecast:
    """Volatility forecast result from EGARCH model."""

    def __init__(
        self,
        ticker: str,
        annualized_vol: float,
        daily_vol: float,
        forecast_1d_low: float,
        forecast_1d_high: float,
        horizon_days: int = 1,
    ) -> None:
        self.ticker = ticker
        self.annualized_vol = annualized_vol
        self.daily_vol = daily_vol
        self.forecast_1d_low = forecast_1d_low
        self.forecast_1d_high = forecast_1d_high
        self.horizon_days = horizon_days

    def direction_range(self, direction: str) -> tuple[float, float]:
        """Return (low_pct, high_pct) move estimate given predicted direction.

        E.g. if direction='short' and daily_vol=1.5%, returns (-3.0, -1.5)
        """
        sigma = self.daily_vol
        if direction in ("strong_long", "long"):
            return (sigma * 0.5, sigma * 2.5)
        elif direction in ("strong_short", "short"):
            return (-sigma * 2.5, -sigma * 0.5)
        else:
            return (-sigma, sigma)

    def __repr__(self) -> str:
        return (
            f"EGARCHForecast(ticker={self.ticker!r}, "
            f"daily_vol={self.daily_vol:.2f}%, "
            f"range=[{self.forecast_1d_low:.2f}%, {self.forecast_1d_high:.2f}%])"
        )


class EGARCHModel:
    """Fit EGARCH(1,1) on price returns and produce volatility forecasts."""

    def __init__(self) -> None:
        self._check_arch()

    def _check_arch(self) -> None:
        try:
            import arch  # type: ignore[import-untyped]  # noqa: F401
        except ImportError:
            logger.warning(
                "EGARCHModel: arch library not installed. "
                "TODO: pip install arch for EGARCH volatility forecasting."
            )

    def _get_returns(self, ticker: str, period: str = "2y") -> pd.Series:
        """Fetch log returns from yfinance."""
        import yfinance as yf

        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        if df.empty or len(df) < 50:
            raise SignalError(f"EGARCHModel: insufficient data for {ticker}")
        prices = df["Close"].dropna()
        log_returns = np.log(prices / prices.shift(1)).dropna() * 100  # in %
        return log_returns

    def fit_and_forecast(
        self,
        ticker: str,
        horizon: int = 5,
        confidence: float = 0.95,
    ) -> EGARCHForecast:
        """Fit EGARCH(1,1) model and forecast volatility.

        Args:
            ticker: Stock symbol
            horizon: Forecast horizon in trading days
            confidence: Confidence interval width (default 0.95 = 95% CI)

        Returns:
            EGARCHForecast with annualized and daily volatility + range
        """
        try:
            from arch import arch_model  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("arch library not available — using rolling std fallback")
            return self._fallback_forecast(ticker, horizon, confidence)

        try:
            returns = self._get_returns(ticker)
            model = arch_model(
                returns,
                vol="EGARCH",
                p=1, q=1,
                mean="AR",
                lags=1,
                dist="normal",
            )
            result = model.fit(
                disp="off",
                options={"maxiter": 200},
                update_freq=0,
            )
            forecast = result.forecast(horizon=horizon, reindex=False)
            variance_forecast = float(forecast.variance.iloc[-1, 0])
            daily_vol = float(np.sqrt(variance_forecast))  # already in % units
            annualized_vol = daily_vol * np.sqrt(252)

            # 95% confidence interval for 1-day move
            z = 1.96 if confidence == 0.95 else 1.645
            low = -z * daily_vol
            high = z * daily_vol

            logger.info(
                "EGARCHModel[%s]: daily_vol=%.2f%% annualized=%.1f%% range=[%.2f%%, %.2f%%]",
                ticker, daily_vol, annualized_vol, low, high,
            )

            return EGARCHForecast(
                ticker=ticker,
                annualized_vol=annualized_vol,
                daily_vol=daily_vol,
                forecast_1d_low=low,
                forecast_1d_high=high,
                horizon_days=horizon,
            )
        except Exception as exc:
            logger.warning("EGARCHModel.fit_and_forecast failed for %s: %s", ticker, exc)
            return self._fallback_forecast(ticker, horizon, confidence)

    def _fallback_forecast(
        self, ticker: str, horizon: int, confidence: float
    ) -> EGARCHForecast:
        """Fallback: use 21-day rolling std when arch is unavailable."""
        try:
            returns = self._get_returns(ticker)
            daily_vol = float(returns.rolling(21).std().iloc[-1])
            annualized_vol = daily_vol * float(np.sqrt(252))
            z = 1.96 if confidence >= 0.95 else 1.645
            return EGARCHForecast(
                ticker=ticker,
                annualized_vol=annualized_vol,
                daily_vol=daily_vol,
                forecast_1d_low=-z * daily_vol,
                forecast_1d_high=z * daily_vol,
                horizon_days=horizon,
            )
        except Exception as exc:
            logger.error("EGARCHModel._fallback_forecast failed for %s: %s", ticker, exc)
            return EGARCHForecast(
                ticker=ticker,
                annualized_vol=20.0,
                daily_vol=1.26,
                forecast_1d_low=-2.5,
                forecast_1d_high=2.5,
                horizon_days=horizon,
            )

    def fit_offline(
        self,
        returns: pd.Series,
        ticker: str = "OFFLINE",
        horizon: int = 1,
    ) -> EGARCHForecast:
        """Fit EGARCH on pre-provided return series (for backtesting)."""
        try:
            from arch import arch_model  # type: ignore[import-untyped]

            model = arch_model(returns, vol="EGARCH", p=1, q=1, mean="Constant", dist="normal")
            result = model.fit(disp="off", options={"maxiter": 200}, update_freq=0)
            forecast = result.forecast(horizon=horizon, reindex=False)
            variance_forecast = float(forecast.variance.iloc[-1, 0])
            daily_vol = float(np.sqrt(variance_forecast))
            annualized_vol = daily_vol * float(np.sqrt(252))
            z = 1.96
            return EGARCHForecast(
                ticker=ticker,
                annualized_vol=annualized_vol,
                daily_vol=daily_vol,
                forecast_1d_low=-z * daily_vol,
                forecast_1d_high=z * daily_vol,
                horizon_days=horizon,
            )
        except ImportError:
            daily_vol = float(returns.std())
            return EGARCHForecast(
                ticker=ticker,
                annualized_vol=daily_vol * float(np.sqrt(252)),
                daily_vol=daily_vol,
                forecast_1d_low=-1.96 * daily_vol,
                forecast_1d_high=1.96 * daily_vol,
                horizon_days=horizon,
            )


__all__ = ["EGARCHModel", "EGARCHForecast"]
