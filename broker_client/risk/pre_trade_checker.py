"""Pre-trade risk check — validates a proposed trade against portfolio limits.

Advisory in paper/backtest mode; hard blocks prevent live execution
when enabled. The check sequence follows the Phase 4 spec order.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from broker_client.buckets.bucket_manager import BucketManager
from broker_client.risk.portfolio_heat_map import SECTOR_MAP
from config.settings import settings
from core.logger import get_logger
from data.db import get_connection

logger = get_logger(__name__)


class ProposedTrade(BaseModel):
    """Minimal description of a trade being considered."""

    ticker: str
    bucket: int = 1
    sub_type: str = "msp"
    option_type: str | None = None   # "put" | "call" | None
    expiry: str | None = None        # YYYY-MM-DD
    margin_estimated: float = 0.0
    premium_paid: float = 0.0        # debit paid (Bucket 3)
    is_on_watchlist: bool = True


class PreTradeResult(BaseModel):
    approved: bool
    hard_blocks: list[str]
    warnings: list[str]
    concentration_after: dict[str, float]
    cash_buffer_after: float
    bucket_capacity_after: dict[int, float]
    recommendation: str  # PROCEED | REVIEW | BLOCK


class PreTradeChecker:
    """Validates proposed trades against portfolio risk limits."""

    def __init__(
        self,
        paper_account: Any = None,
        bucket_manager: BucketManager | None = None,
        db_path: str | None = None,
    ) -> None:
        self._paper = paper_account
        self._buckets = bucket_manager or BucketManager(paper_account=paper_account)
        self._db_path = db_path or settings.DB_PATH

    def check(
        self,
        trade: ProposedTrade,
        positions: list[Any],
        portfolio_value: float | None = None,
    ) -> PreTradeResult:
        """Run all pre-trade checks and return a PreTradeResult."""
        pv = portfolio_value or self._equity()
        blocks: list[str] = []
        warnings: list[str] = []

        # 1. Cash buffer check
        cash_after = self._cash_after(trade.margin_estimated)
        cash_pct_after = cash_after / pv * 100.0 if pv > 0 else 0.0
        if cash_pct_after < settings.CASH_BUFFER_CRITICAL_PCT:
            blocks.append(
                f"Insufficient cash: {cash_pct_after:.1f}% remaining "
                f"(minimum {settings.CASH_BUFFER_CRITICAL_PCT:.0f}%)"
            )
        elif cash_pct_after < settings.CASH_BUFFER_WARNING_PCT:
            warnings.append(
                f"Cash buffer low after trade: {cash_pct_after:.1f}%"
            )

        # 2. Bucket capacity check
        bucket_exp = self._bucket_exposure(trade.bucket)
        bucket_max = pv * self._allocation_pct(trade.bucket) / 100.0
        if bucket_max > 0 and (bucket_exp + trade.margin_estimated) > bucket_max:
            blocks.append(
                f"Bucket {trade.bucket} capacity exceeded: "
                f"${bucket_exp:,.0f} + ${trade.margin_estimated:,.0f} "
                f"> ${bucket_max:,.0f} limit"
            )

        # 3. Sector concentration check
        sector = SECTOR_MAP.get(trade.ticker, "Other")
        sector_after = self._sector_after(trade, positions, pv)
        if sector_after > settings.SECTOR_CRITICAL_PCT:
            blocks.append(
                f"Sector concentration critical after trade: "
                f"{sector} would be {sector_after:.1f}%"
            )
        elif sector_after > settings.SECTOR_WARNING_PCT:
            warnings.append(
                f"Sector concentration warning: "
                f"{sector} would be {sector_after:.1f}%"
            )

        # 4. Same-expiry check
        if trade.expiry:
            same_count = self._count_same_expiry_week(positions, trade.expiry)
            if same_count + 1 >= settings.SAME_EXPIRY_WARNING:
                warnings.append(
                    f"Expiry concentration: {same_count + 1} positions "
                    f"would expire week of {trade.expiry}"
                )

        # 5. Earnings proximity check
        days_to_earn = self._days_to_earnings(trade.ticker)
        if days_to_earn is not None and days_to_earn < 14:
            if trade.bucket != 2:
                warnings.append(
                    f"{trade.ticker} reports earnings in {days_to_earn} days "
                    "— consider this before opening a non-earnings position"
                )

        # 6. Watchlist / assignment readiness (Bucket 1)
        if trade.bucket == 1 and not trade.is_on_watchlist:
            warnings.append(
                f"{trade.ticker} is not on watchlist — "
                "are you comfortable owning it at assignment?"
            )

        # 7. Bucket 3 single-trade size limit
        if trade.bucket == 3 and pv > 0:
            max_b3 = pv * settings.BUCKET3_MAX_SINGLE_TRADE_PCT / 100.0
            if trade.premium_paid > max_b3:
                blocks.append(
                    f"Bucket 3 single trade limit exceeded: "
                    f"${trade.premium_paid:,.0f} > ${max_b3:,.0f} "
                    f"({settings.BUCKET3_MAX_SINGLE_TRADE_PCT:.0f}% limit)"
                )

        bucket_after = {
            b: self._bucket_exposure(b) + (trade.margin_estimated if b == trade.bucket else 0.0)
            for b in (1, 2, 3)
        }

        return PreTradeResult(
            approved=len(blocks) == 0,
            hard_blocks=blocks,
            warnings=warnings,
            concentration_after={"sector": round(sector_after, 2)},
            cash_buffer_after=round(cash_pct_after, 2),
            bucket_capacity_after={b: round(v, 2) for b, v in bucket_after.items()},
            recommendation="BLOCK" if blocks else "REVIEW" if warnings else "PROCEED",
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _equity(self) -> float:
        if self._paper is None:
            return 0.0
        try:
            return float(self._paper.get_state().equity or 0)
        except Exception:
            return 0.0

    def _cash_after(self, margin: float) -> float:
        if self._paper is None:
            return 0.0
        try:
            cash = float(self._paper.get_state().cash or 0)
            return cash - margin
        except Exception:
            return 0.0

    def _allocation_pct(self, bucket: int) -> int:
        return {
            1: settings.BUCKET1_ALLOCATION_PCT,
            2: settings.BUCKET2_ALLOCATION_PCT,
            3: settings.BUCKET3_ALLOCATION_PCT,
        }.get(bucket, 0)

    def _bucket_exposure(self, bucket: int) -> float:
        rows = self._buckets.get_bucket_positions(bucket)
        total = 0.0
        for row in rows:
            total += abs(row.get("entry_price") or 0.0) * abs(row.get("qty") or 0)
        return total

    def _sector_after(self, trade: ProposedTrade, positions: list[Any], pv: float) -> float:
        """Sector concentration as % of portfolio_value (not just existing exposure)."""
        if pv <= 0:
            return 0.0
        sector = SECTOR_MAP.get(trade.ticker, "Other")
        current_sector_exp = 0.0
        for pos in positions:
            from broker_client.risk.portfolio_heat_map import _get as _hget
            t = str(_hget(pos, "ticker", "") or "")
            entry = abs(float(_hget(pos, "entry_price", 0) or 0))
            qty = abs(int(_hget(pos, "qty", 1) or 1))
            is_opt = _hget(pos, "option_type") in ("put", "call")
            mult = settings.OPTIONS_CONTRACT_MULTIPLIER if is_opt else 1
            if SECTOR_MAP.get(t, "Other") == sector:
                current_sector_exp += entry * qty * mult
        return (current_sector_exp + trade.margin_estimated) / pv * 100.0

    def _count_same_expiry_week(self, positions: list[Any], expiry: str) -> int:
        try:
            target = date.fromisoformat(expiry)
        except (ValueError, TypeError):
            return 0
        target_week = target.isocalendar()[1]
        target_year = target.year
        count = 0
        for pos in positions:
            from broker_client.risk.portfolio_heat_map import _get as _hget
            exp_str = _hget(pos, "expiry")
            if not exp_str:
                continue
            try:
                exp = date.fromisoformat(str(exp_str))
                if exp.year == target_year and exp.isocalendar()[1] == target_week:
                    count += 1
            except (ValueError, TypeError):
                continue
        return count

    def _days_to_earnings(self, ticker: str) -> int | None:
        try:
            with get_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT earnings_date FROM earnings_opportunities "
                    "WHERE ticker=? ORDER BY earnings_date LIMIT 1",
                    (ticker,),
                ).fetchone()
            if row and row["earnings_date"]:
                earn_date = date.fromisoformat(row["earnings_date"])
                return (earn_date - date.today()).days
        except Exception as exc:
            logger.debug("PreTradeChecker: earnings lookup failed for %s: %s", ticker, exc)
        return None


__all__ = ["PreTradeChecker", "PreTradeResult", "ProposedTrade"]
