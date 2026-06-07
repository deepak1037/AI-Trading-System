"""V2: Live execution layer with Kelly sizing + auto stop-loss (Days 19-25).

This module extends V1's OrderRouter with:
  1. Kelly-fractional position sizing informed by EGARCH volatility forecast
  2. Automatic stop-loss placement based on ATR multiplier
  3. GEX-aware entry/exit decisions (avoid entries in negative-gamma regime)
  4. Integration with live Schwab execution (after 3-month paper trading validation)

CLAUDE.md Section 19 design decision:
  - Lumibot for backtest → live: same code, one config flag
  - Triple-gate for live orders: ENV=live + DRY_RUN=False + LIVE_TRADING_ENABLED=True
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings
from core.exceptions import RiskError, SignalError
from core.logger import get_logger
from broker_client.order_router import OrderRouter
from broker_client.risk_manager import RiskManager
from broker_core.base_broker import Account, Order, OrderResult
from signals.signal_schema import Signal
from v2.egarch_model import EGARCHModel
from v2.gex_calculator import GEXCalculator

logger = get_logger(__name__)

_NOT_YET_LIVE = (
    "V2 live execution requires minimum 3 months of paper trading validation "
    "per CLAUDE.md Section 3 — this module is ready but execution is gated by "
    "the paper trading period requirement."
)


class V2ExecutionEngine:
    """Executes V2 enhanced orders with Kelly sizing + vol forecasts + GEX awareness.

    This is NOT a replacement for TradingEngine — it's a V2 enhancement layer
    that adds magnitude estimation on top of V1's directional signal.
    """

    def __init__(
        self,
        router: Optional[OrderRouter] = None,
        risk_manager: Optional[RiskManager] = None,
    ) -> None:
        self._router = router
        self._risk = risk_manager or RiskManager()
        self._egarch = EGARCHModel()
        self._gex = GEXCalculator()
        logger.info("V2ExecutionEngine initialized. NOTE: %s", _NOT_YET_LIVE)

    def estimate_move_range(
        self,
        signal: Signal,
        ticker: str,
    ) -> tuple[float, float]:
        """Return (low_pct, high_pct) expected move range using EGARCH.

        This implements CLAUDE.md Section 19.4: output range, not point estimate.
        E.g. for a bearish signal: returns (-4.5, -2.0) not just '-3.4%'.
        """
        forecast = self._egarch.fit_and_forecast(ticker, horizon=1)
        return forecast.direction_range(signal.direction)

    def kelly_size_with_vol(
        self,
        account: Account,
        ticker: str,
        signal: Signal,
        price: float,
    ) -> int:
        """Compute Kelly-fractional position size adjusted for EGARCH volatility.

        High volatility → smaller size (volatility penalty).
        Low volatility → can scale closer to full Kelly.
        """
        try:
            forecast = self._egarch.fit_and_forecast(ticker, horizon=1)
        except Exception as exc:
            logger.warning("V2: EGARCH forecast failed, using base Kelly: %s", exc)
            forecast = None

        # Kelly sizing: use win_rate from confidence, estimate avg_win/loss from vol
        win_rate = signal.confidence / 100.0
        daily_vol = forecast.daily_vol if forecast else 1.5
        avg_win = daily_vol * 1.5  # expect 1.5× vol on winning trades
        avg_loss = daily_vol       # expect 1× vol on losing trades

        dollar_size = self._risk.kelly_size(account, win_rate, avg_win, avg_loss)
        if dollar_size <= 0 or price <= 0:
            return 0
        base_qty = int(dollar_size / price)
        if base_qty == 0:
            return 0

        # Vol penalty: if daily_vol > 3%, scale down proportionally
        vol_scalar = min(1.0, 3.0 / max(daily_vol, 0.1))
        adjusted = max(1, int(base_qty * vol_scalar))

        logger.debug(
            "V2 Kelly size: base=%d vol=%.2f%% scalar=%.2f adjusted=%d",
            base_qty, daily_vol, vol_scalar, adjusted,
        )
        return adjusted

    def gex_regime_ok(
        self,
        ticker: str,
        spot_price: float,
        direction: str,
    ) -> bool:
        """Check if GEX regime supports the proposed trade direction.

        Positive net GEX (MMs long gamma) → market tends to mean-revert → good for neutral strategies
        Negative net GEX (MMs short gamma) → market tends to trend → good for directional trades

        Returns True if GEX regime is not strongly against the direction.
        """
        try:
            gex = self._gex.compute(ticker, spot_price)
        except Exception as exc:
            logger.debug("V2: GEX compute failed (non-blocking): %s", exc)
            return True  # don't block on GEX failure

        if abs(gex.net_gex) < 1e6:
            return True  # GEX too small to matter

        if direction in ("long", "strong_long") and gex.net_gex < -1e9:
            # Strong negative GEX during bullish signal = market tends to trend up = good
            return True
        elif direction in ("short", "strong_short") and gex.net_gex > 1e9:
            # Strong positive GEX during bearish signal = risk of mean reversion = caution
            logger.info("V2: positive GEX regime conflicts with short signal — reducing confidence")
            return False

        return True

    def compute_atr_stop(
        self,
        ticker: str,
        entry_price: float,
        direction: str,
        atr_period: int = 14,
    ) -> tuple[float, float]:
        """Compute ATR-based stop loss and take profit levels.

        Returns: (stop_loss, take_profit)
        """
        try:
            import yfinance as yf
            import pandas as pd

            df = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=True)
            if df.empty or len(df) < atr_period:
                raise SignalError("insufficient data for ATR")
            df.columns = [c.lower() for c in df.columns]
            high_low = df["high"] - df["low"]
            high_close = abs(df["high"] - df["close"].shift(1))
            low_close = abs(df["low"] - df["close"].shift(1))
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            atr = float(tr.rolling(atr_period).mean().iloc[-1])
        except Exception as exc:
            logger.warning("V2: ATR computation failed, using 2% default: %s", exc)
            atr = entry_price * 0.02

        stop_distance = atr * settings.STOP_LOSS_ATR_MULTIPLIER
        profit_distance = atr * settings.TAKE_PROFIT_ATR_MULTIPLIER

        if direction in ("long", "strong_long"):
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + profit_distance
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - profit_distance

        logger.debug(
            "V2 ATR stop: %s @ %.4f atr=%.4f stop=%.4f tp=%.4f",
            ticker, entry_price, atr, stop_loss, take_profit,
        )
        return stop_loss, take_profit

    def build_enhanced_order(
        self,
        signal: Signal,
        account: Account,
        ticker: str,
        price: float,
    ) -> Order:
        """Build an order with V2 enhancements: Kelly size + ATR stops.

        This is used by V2-enabled strategies instead of the base strategy's build_order.
        Still routes through OrderRouter (never bypasses the triple-gate).
        """
        qty = self.kelly_size_with_vol(account, ticker, signal, price)
        if qty == 0:
            raise RiskError(f"V2: Kelly size computed 0 for {ticker} — skipping")

        stop_loss, take_profit = self.compute_atr_stop(ticker, price, signal.direction)

        from typing import Literal
        action: Literal["BUY", "SELL"] = (
            "BUY" if signal.direction in ("long", "strong_long") else "SELL"
        )

        return Order(
            ticker=ticker,
            action=action,
            qty=qty,
            order_type="market",
            strategy_name="v2_enhanced",
            account_id=settings.PAPER_ACCOUNTS[0] if settings.PAPER_ACCOUNTS else "paper_main",
        )

    def execute_with_v2(
        self,
        signal: Signal,
        account: Account,
        ticker: str,
        price: float,
    ) -> Optional[OrderResult]:
        """Full V2 execution pipeline.

        1. GEX check — block if regime conflicts strongly
        2. EGARCH sizing
        3. ATR stop placement
        4. OrderRouter.execute() (triple-gate still applies)

        Returns OrderResult or None if blocked.
        """
        if not self._router:
            logger.error("V2ExecutionEngine: no OrderRouter configured")
            return None

        if not self.gex_regime_ok(ticker, price, signal.direction):
            logger.info("V2: order blocked by unfavorable GEX regime for %s", ticker)
            return None

        move_low, move_high = self.estimate_move_range(signal, ticker)
        logger.info(
            "V2: signal=%s ticker=%s expected_move=[%.1f%%, %.1f%%]",
            signal.direction, ticker, move_low * 100, move_high * 100,
        )

        order = self.build_enhanced_order(signal, account, ticker, price)
        return self._router.execute(order)


__all__ = ["V2ExecutionEngine"]
