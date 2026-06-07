"""Performance metrics for paper trading accounts (Day 7).

Computes Sharpe, Sortino, max drawdown, win rate, and profit factor
from a trade history CSV or list of trade records.
"""

from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel

from core.logger import get_logger

logger = get_logger(__name__)

_RISK_FREE_RATE = 0.05  # 5% annual, default
_TRADING_DAYS = 252


class PerformanceMetrics(BaseModel):
    """Complete performance metric set for an account or strategy."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    max_drawdown_pct: float = 0.0
    calmar_ratio: Optional[float] = None
    annualized_return_pct: Optional[float] = None


def compute_metrics(
    pnl_series: list[float],
    risk_free_rate: float = _RISK_FREE_RATE,
) -> PerformanceMetrics:
    """Compute full performance metrics from a list of per-trade P&L values.

    Args:
        pnl_series: List of realized P&L values per trade (positive=win, negative=loss).
        risk_free_rate: Annual risk-free rate (default 5%).

    Returns:
        PerformanceMetrics pydantic model.
    """
    if not pnl_series:
        return PerformanceMetrics()

    total_trades = len(pnl_series)
    wins = [p for p in pnl_series if p > 0]
    losses = [p for p in pnl_series if p < 0]

    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    total_pnl = sum(pnl_series)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe ratio (daily returns approximation)
    if len(pnl_series) > 1:
        mean_pnl = total_pnl / total_trades
        std_pnl = _std(pnl_series)
        daily_rf = risk_free_rate / _TRADING_DAYS
        sharpe = (mean_pnl - daily_rf) / std_pnl if std_pnl > 0 else None
        sharpe = sharpe * math.sqrt(_TRADING_DAYS) if sharpe is not None else None
    else:
        sharpe = None

    # Sortino ratio (downside deviation only)
    negative_returns = [p for p in pnl_series if p < 0]
    if negative_returns and len(pnl_series) > 1:
        downside_std = _std(negative_returns)
        mean_pnl = total_pnl / total_trades
        daily_rf = risk_free_rate / _TRADING_DAYS
        sortino = (mean_pnl - daily_rf) / downside_std * math.sqrt(_TRADING_DAYS) if downside_std > 0 else None
    else:
        sortino = None

    # Max drawdown
    max_dd = _max_drawdown(pnl_series)

    return PerformanceMetrics(
        total_trades=total_trades,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=round(win_rate * 100, 2),
        avg_win=round(avg_win, 4),
        avg_loss=round(avg_loss, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        total_pnl=round(total_pnl, 4),
        sharpe_ratio=round(sharpe, 4) if sharpe is not None else None,
        sortino_ratio=round(sortino, 4) if sortino is not None else None,
        max_drawdown_pct=round(max_dd * 100, 4),
    )


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _max_drawdown(pnl_series: list[float]) -> float:
    """Return max drawdown as a fraction (e.g. 0.15 = 15% drawdown)."""
    if not pnl_series:
        return 0.0

    # Build cumulative equity curve starting from 0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for pnl in pnl_series:
        equity += pnl
        if equity > peak:
            peak = equity
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        if drawdown > max_dd:
            max_dd = drawdown

    return max_dd


__all__ = ["PerformanceMetrics", "compute_metrics"]
