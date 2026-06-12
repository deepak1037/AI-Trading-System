"""Shared models for the three-bucket framework (Phase 2).

``BucketPosition`` is the enriched, framework-aware view of an open position
consumed by the exit-rules engine, the 21-DTE alert system, and the LLM exit
engine. It deliberately lives in its own module so those three consumers can
import it without creating an import cycle through ``bucket_manager``.
"""

from __future__ import annotations

from pydantic import BaseModel

from config.settings import settings

# Per-contract share multiplier for option notionals.
_OPTION_MULT = settings.OPTIONS_CONTRACT_MULTIPLIER


class BucketPosition(BaseModel):
    """A position annotated with everything the exit logic needs.

    ``profit_pct`` is the unrealized P&L as a percentage of the entry basis,
    signed so that profit is positive. For a short option the basis is the
    premium collected (collect $1.00, now worth $0.50 → +50%); for a long
    option it is the debit paid (pay $1.00, now worth $1.40 → +40%).
    """

    ticker: str
    strategy: str = ""                     # raw strategy name (pre-classification)
    bucket: int = 1                        # 1 | 2 | 3
    sub_type: str = "msp"
    recoverable: bool = True
    position_type: str = "equity_long"
    option_type: str | None = None      # "put" | "call" | None (equity)
    strike: float | None = None
    expiry: str | None = None           # YYYY-MM-DD
    qty: int = 1

    entry_price: float = 0.0               # premium collected (short) / debit (long)
    current_price: float = 0.0             # current option/share price
    underlying_price: float | None = None

    profit_pct: float = 0.0                # unrealized P&L %, profit positive
    dte_remaining: int = 0
    original_dte: int = 0
    days_held: int = 0

    delta: float | None = None
    iv_rank: float | None = None
    iv_at_entry: float | None = None

    entry_thesis: str | None = None
    thesis_status: str = "active"          # active | partial | complete | broken
    thesis_completion_pct: float = 0.0

    position_id: int | None = None
    earnings_date: str | None = None    # YYYY-MM-DD if known

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def dte_used_pct(self) -> float:
        """Percentage of the originally-bought time that has elapsed (0–100)."""
        if self.original_dte <= 0:
            return 0.0
        used = (self.original_dte - self.dte_remaining) / self.original_dte * 100.0
        return max(0.0, min(used, 100.0))

    @property
    def loss_pct(self) -> float:
        """Magnitude of loss as a positive percent (0 when in profit)."""
        return max(-self.profit_pct, 0.0)

    @property
    def is_option(self) -> bool:
        return self.option_type in ("put", "call")

    @property
    def is_itm(self) -> bool:
        """True when an option is in-the-money. Equities/unknown → False."""
        if self.option_type is None or self.strike is None or self.underlying_price is None:
            return False
        if self.option_type == "put":
            return self.underlying_price < self.strike
        return self.underlying_price > self.strike

    @property
    def position_value(self) -> float:
        """Capital proxy: |entry_price| × |qty| (× 100 for options)."""
        mult = _OPTION_MULT if self.is_option else 1
        return abs(self.entry_price) * abs(self.qty) * mult


__all__ = ["BucketPosition"]
