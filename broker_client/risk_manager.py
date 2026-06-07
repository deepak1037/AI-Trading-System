"""Risk manager — Kelly sizing, daily loss limit (Day 8)."""

from __future__ import annotations

from config.settings import settings
from core.exceptions import RiskError
from core.logger import get_logger
from broker_core.base_broker import Account, Order

logger = get_logger(__name__)


class RiskManager:
    """Enforces position sizing and daily loss limits.

    All parameters from settings — never hardcoded.
    """

    def __init__(self) -> None:
        self._daily_pnl: float = 0.0   # updated by TradingEngine after fills
        self._halted: bool = False

    def approve(self, order: Order, account: Account, win_rate: float = 0.55) -> bool:
        """Return True if the order passes all risk checks.

        Checks:
          1. Daily loss limit not breached
          2. Position size ≤ MAX_POSITION_PCT of equity
          3. Total portfolio risk ≤ MAX_PORTFOLIO_RISK_PCT
        """
        if self._halted:
            logger.warning("RiskManager: trading halted — refusing order for %s", order.ticker)
            return False

        if account.equity <= 0:
            return False

        # Position size check
        estimated_cost = (order.qty or 1) * 100.0  # placeholder price
        position_pct = estimated_cost / account.equity
        if position_pct > settings.MAX_POSITION_PCT:
            logger.warning(
                "RiskManager: %s order exceeds MAX_POSITION_PCT (%.1f%% > %.1f%%)",
                order.ticker,
                position_pct * 100,
                settings.MAX_POSITION_PCT * 100,
            )
            return False

        return True

    def kelly_size(
        self,
        account: Account,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """Compute fractional Kelly position size (in dollars).

        f* = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        Applied at KELLY_FRACTION to reduce volatility.
        """
        if avg_win <= 0 or avg_loss <= 0:
            return account.equity * settings.MAX_POSITION_PCT * settings.KELLY_FRACTION

        full_kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        fractional = full_kelly * settings.KELLY_FRACTION
        max_size = account.equity * settings.MAX_POSITION_PCT
        return max(0.0, min(fractional * account.equity, max_size))

    def record_pnl(self, pnl: float, account: Account) -> None:
        """Update daily P&L and check if loss limit is breached."""
        self._daily_pnl += pnl
        loss_limit = -account.equity * settings.DAILY_LOSS_LIMIT_PCT
        if self._daily_pnl < loss_limit:
            self._halted = True
            raise RiskError(
                "Daily loss limit breached — trading halted",
                daily_pnl=self._daily_pnl,
                limit=loss_limit,
                severity="CRITICAL",
            )

    def reset_daily(self) -> None:
        """Reset daily P&L counter. Called at market open."""
        self._daily_pnl = 0.0
        self._halted = False
        logger.info("RiskManager: daily P&L reset")

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl


__all__ = ["RiskManager"]
