"""Performance attribution — P&L by bucket, strategy, and signal source.

Reads from the positions table (closed trades) and computes win-rate,
profit-factor, avg-hold, and benchmark-relative return. Requires at least
MIN_TRADES_FOR_ATTRIBUTION closed trades, otherwise returns an empty report
with insufficient_data=True so callers can show a placeholder gracefully.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger
from data.db import get_connection

logger = get_logger(__name__)


class BucketAttribution(BaseModel):
    bucket: int
    realized_pnl: float
    unrealized_pnl: float = 0.0
    total_trades: int
    win_rate: float
    avg_hold_days: float
    premium_collected: float


class StrategyAttribution(BaseModel):
    strategy: str
    realized_pnl: float
    trade_count: int
    win_rate: float
    avg_return_pct: float
    avg_hold_days: float


class TradeRecord(BaseModel):
    ticker: str
    strategy: str
    realized_pnl: float
    opened_at: str
    closed_at: str
    bucket: int = 1


class AttributionReport(BaseModel):
    period: str
    insufficient_data: bool = False
    total_pnl: float
    total_return_pct: float

    bucket_attribution: dict[int, BucketAttribution]
    best_bucket: int
    worst_bucket: int

    strategy_attribution: dict[str, StrategyAttribution]
    best_strategy: str
    worst_strategy: str

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_winner_pct: float
    avg_loser_pct: float
    profit_factor: float

    best_trade: TradeRecord | None
    worst_trade: TradeRecord | None

    vs_spy: float = 0.0


class PerformanceAttribution:
    """Generates attribution reports from closed-position history."""

    def __init__(self, db_path: str | None = None, paper_account: Any = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._paper = paper_account

    def generate_report(self, period: str = "MTD") -> AttributionReport:
        """Build an attribution report for the given period (MTD/QTD/YTD/30D)."""
        start = self._period_start(period)
        trades = self._load_closed_trades(start)

        if len(trades) < settings.MIN_TRADES_FOR_ATTRIBUTION:
            logger.info(
                "PerformanceAttribution: only %d closed trades — minimum is %d",
                len(trades), settings.MIN_TRADES_FOR_ATTRIBUTION,
            )
            return self._empty_report(period)

        equity = self._equity()
        total_pnl = sum(t["realized_pnl"] for t in trades)
        total_return_pct = total_pnl / equity * 100.0 if equity > 0 else 0.0

        bucket_attr = self._bucket_attribution(trades)
        strategy_attr = self._strategy_attribution(trades)
        win_trades = [t for t in trades if t["realized_pnl"] > 0]
        loss_trades = [t for t in trades if t["realized_pnl"] <= 0]
        win_rate = len(win_trades) / len(trades) * 100.0

        avg_win_pct = self._avg_pct(win_trades)
        avg_loss_pct = self._avg_pct(loss_trades)
        profit_factor = self._profit_factor(trades)

        best = max(trades, key=lambda t: t["realized_pnl"])
        worst = min(trades, key=lambda t: t["realized_pnl"])
        best_trade = TradeRecord(
            ticker=best["ticker"],
            strategy=best["strategy"],
            realized_pnl=best["realized_pnl"],
            opened_at=best.get("opened_at", ""),
            closed_at=best.get("closed_at", ""),
            bucket=best.get("bucket", 1),
        )
        worst_trade = TradeRecord(
            ticker=worst["ticker"],
            strategy=worst["strategy"],
            realized_pnl=worst["realized_pnl"],
            opened_at=worst.get("opened_at", ""),
            closed_at=worst.get("closed_at", ""),
            bucket=worst.get("bucket", 1),
        )

        best_bucket = max(bucket_attr, key=lambda b: bucket_attr[b].realized_pnl, default=1)
        worst_bucket = min(bucket_attr, key=lambda b: bucket_attr[b].realized_pnl, default=1)
        best_strat = max(strategy_attr, key=lambda s: strategy_attr[s].realized_pnl, default="")
        worst_strat = min(strategy_attr, key=lambda s: strategy_attr[s].realized_pnl, default="")

        vs_spy = self._vs_spy(period, total_return_pct)

        return AttributionReport(
            period=period,
            insufficient_data=False,
            total_pnl=round(total_pnl, 2),
            total_return_pct=round(total_return_pct, 4),
            bucket_attribution=bucket_attr,
            best_bucket=best_bucket,
            worst_bucket=worst_bucket,
            strategy_attribution=strategy_attr,
            best_strategy=best_strat,
            worst_strategy=worst_strat,
            total_trades=len(trades),
            winning_trades=len(win_trades),
            losing_trades=len(loss_trades),
            win_rate=round(win_rate, 2),
            avg_winner_pct=round(avg_win_pct, 4),
            avg_loser_pct=round(avg_loss_pct, 4),
            profit_factor=round(profit_factor, 4),
            best_trade=best_trade,
            worst_trade=worst_trade,
            vs_spy=round(vs_spy, 4),
        )

    # ── sub-aggregations ─────────────────────────────────────────────────────

    def _bucket_attribution(self, trades: list[dict]) -> dict[int, BucketAttribution]:
        result: dict[int, BucketAttribution] = {}
        for b in (1, 2, 3):
            bt = [t for t in trades if t.get("bucket", 1) == b]
            if not bt:
                result[b] = BucketAttribution(
                    bucket=b, realized_pnl=0.0, total_trades=0,
                    win_rate=0.0, avg_hold_days=0.0, premium_collected=0.0,
                )
                continue
            wins = [t for t in bt if t["realized_pnl"] > 0]
            result[b] = BucketAttribution(
                bucket=b,
                realized_pnl=round(sum(t["realized_pnl"] for t in bt), 2),
                total_trades=len(bt),
                win_rate=round(len(wins) / len(bt) * 100.0, 2),
                avg_hold_days=round(self._avg_hold(bt), 2),
                premium_collected=round(sum(abs(t.get("fill_price", 0)) for t in bt), 2),
            )
        return result

    def _strategy_attribution(self, trades: list[dict]) -> dict[str, StrategyAttribution]:
        by_strat: dict[str, list[dict]] = {}
        for t in trades:
            s = t.get("strategy", "unknown")
            by_strat.setdefault(s, []).append(t)
        result = {}
        for strat, st in by_strat.items():
            wins = [t for t in st if t["realized_pnl"] > 0]
            result[strat] = StrategyAttribution(
                strategy=strat,
                realized_pnl=round(sum(t["realized_pnl"] for t in st), 2),
                trade_count=len(st),
                win_rate=round(len(wins) / len(st) * 100.0, 2),
                avg_return_pct=self._avg_pct(st),
                avg_hold_days=round(self._avg_hold(st), 2),
            )
        return result

    # ── data loading ─────────────────────────────────────────────────────────

    def _load_closed_trades(self, start: date) -> list[dict]:
        try:
            with get_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT ticker, strategy, realized_pnl, opened_at, closed_at, "
                    "bucket, entry_price AS fill_price "
                    "FROM positions "
                    "WHERE is_open=0 AND realized_pnl IS NOT NULL "
                    "AND closed_at >= ? "
                    "ORDER BY closed_at",
                    (start.isoformat(),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("PerformanceAttribution: DB read failed: %s", exc)
            return []

    # ── metrics ──────────────────────────────────────────────────────────────

    @staticmethod
    def _profit_factor(trades: list[dict]) -> float:
        gross_profit = sum(t["realized_pnl"] for t in trades if t["realized_pnl"] > 0)
        gross_loss = abs(sum(t["realized_pnl"] for t in trades if t["realized_pnl"] < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @staticmethod
    def _avg_pct(trades: list[dict]) -> float:
        if not trades:
            return 0.0
        pcts = []
        for t in trades:
            pnl = t.get("realized_pnl", 0) or 0
            entry = abs(float(t.get("fill_price", 0) or 0))
            if entry > 0:
                pcts.append(pnl / entry * 100.0)
        return sum(pcts) / len(pcts) if pcts else 0.0

    @staticmethod
    def _avg_hold(trades: list[dict]) -> float:
        days = []
        for t in trades:
            try:
                o = datetime.fromisoformat(t["opened_at"])
                c = datetime.fromisoformat(t["closed_at"])
                days.append((c - o).days)
            except Exception:
                pass
        return sum(days) / len(days) if days else 0.0

    # ── benchmark ────────────────────────────────────────────────────────────

    def _vs_spy(self, period: str, system_return: float) -> float:
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            start = self._period_start(period)
            hist = yf.Ticker(settings.DASHBOARD_BENCHMARK_TICKER).history(
                start=start.isoformat(), end=date.today().isoformat()
            )
            if hist is None or len(hist) < 2:
                return 0.0
            spy_ret = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100.0
            return system_return - spy_ret
        except Exception as exc:
            logger.debug("PerformanceAttribution: SPY benchmark failed: %s", exc)
            return 0.0

    # ── helpers ──────────────────────────────────────────────────────────────

    def _equity(self) -> float:
        if self._paper is None:
            return 0.0
        try:
            return float(self._paper.get_state().equity or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _period_start(period: str) -> date:
        today = date.today()
        if period == "MTD":
            return today.replace(day=1)
        if period == "QTD":
            month = ((today.month - 1) // 3) * 3 + 1
            return today.replace(month=month, day=1)
        if period == "YTD":
            return today.replace(month=1, day=1)
        if period == "30D":
            return today - timedelta(days=settings.PERFORMANCE_LOOKBACK_DAYS)
        return today - timedelta(days=settings.PERFORMANCE_LOOKBACK_DAYS)

    @staticmethod
    def _empty_report(period: str) -> AttributionReport:
        empty_bucket = {
            b: BucketAttribution(
                bucket=b, realized_pnl=0.0, total_trades=0,
                win_rate=0.0, avg_hold_days=0.0, premium_collected=0.0,
            )
            for b in (1, 2, 3)
        }
        return AttributionReport(
            period=period,
            insufficient_data=True,
            total_pnl=0.0,
            total_return_pct=0.0,
            bucket_attribution=empty_bucket,
            best_bucket=1, worst_bucket=1,
            strategy_attribution={},
            best_strategy="", worst_strategy="",
            total_trades=0,
            winning_trades=0, losing_trades=0,
            win_rate=0.0, avg_winner_pct=0.0, avg_loser_pct=0.0,
            profit_factor=0.0,
            best_trade=None, worst_trade=None,
        )


__all__ = [
    "PerformanceAttribution",
    "AttributionReport",
    "BucketAttribution",
    "StrategyAttribution",
    "TradeRecord",
]
