"""Shared pydantic models for the earnings analyzer (Phase 3, Module A).

These live in one module so the connector, IV analyzer, strategy builder, LLM
assessor, and scanner can all import them without an import cycle (same pattern
as ``broker_client/buckets/models.py``).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator

# Strategy / priority vocabularies (shared so callers compare against constants).
IV_CRUSH = "IV_CRUSH"
IV_SPIKE = "IV_SPIKE"
SKIP = "SKIP"

PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_LOW = "LOW"
PRIORITY_SKIP = "SKIP"


class QuarterlyData(BaseModel):
    """One historical earnings quarter, mirroring Moomoo's per-quarter table.

    Every metric is optional because real exports often have gaps; the analyzer
    skips ``None`` fields rather than guessing.
    """

    quarter: str = ""                       # e.g. "2025Q1"
    earnings_date: date | None = None
    iv_before: float | None = None          # IV % shortly before earnings
    iv_after: float | None = None           # IV % shortly after earnings
    iv_crush: float | None = None           # IV drop after earnings (%, positive)
    expected_move: float | None = None      # ±% the market priced in
    actual_move_close: float | None = None  # actual move from prior close (%, signed)

    # Optional OHLC context around the event (Moomoo "Historical" table).
    before_14d: float | None = None
    before_1d: float | None = None
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    close_price: float | None = None
    after_1d: float | None = None
    after_14d: float | None = None


class EarningsIVHistory(BaseModel):
    """Per-quarter IV/move history for one ticker (detail view in Moomoo)."""

    ticker: str
    quarters: list[QuarterlyData] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.quarters)


class EarningsEvent(BaseModel):
    """An upcoming earnings event with current IV context (Moomoo screen row)."""

    ticker: str
    earnings_date: date
    earnings_time: str = "AMC"          # "BMO" before open | "AMC" after close
    iv_current: float = 0.0             # current IV %
    iv_rank: int = 0                    # 0-100
    iv_percentile: int = 0              # 0-100
    last_iv_crush: float = 0.0          # IV drop after last earnings %
    hist_iv_crush: float = 0.0          # avg IV crush across history %
    expected_move: float = 0.0          # ±% expected move
    last_earnings_move: float = 0.0     # actual move last quarter %
    hist_earnings_move: float = 0.0     # avg actual vs expected %
    forecast_revenue_yoy: float | None = None
    forecast_eps_yoy: float | None = None
    options_volume: int | None = None
    open_interest: int | None = None
    stock_price: float | None = None
    source: str = "manual"              # moomoo | manual | fmp | yfinance


class IVAnalysis(BaseModel):
    """Result of analysing a ticker's IV-crush / breach-rate history."""

    ticker: str

    # IV Crush analysis
    avg_iv_crush: float = 0.0
    last_iv_crush: float = 0.0
    iv_crush_consistency: float = 0.0   # frac of quarters with meaningful crush

    # Expected move analysis
    avg_expected_move: float = 0.0
    avg_actual_move: float = 0.0
    breach_rate: float = 0.0
    breach_rate_upper: float = 0.0
    breach_rate_lower: float = 0.0
    safe_move_level: float = 0.0        # percentile move the strike must clear (%)

    # Current setup
    iv_rank_current: int = 0
    iv_percentile_current: int = 0
    expected_move_current: float = 0.0

    quarters_analyzed: int = 0

    # Recommendation
    strategy: str = SKIP                # IV_CRUSH | IV_SPIKE | SKIP
    confidence: int = 0
    reasoning: str = ""


class IVCrushTrade(BaseModel):
    """An IV-crush (sell premium) trade recommendation."""

    ticker: str
    strategy: str = IV_CRUSH
    earnings_date: date
    earnings_time: str = "AMC"

    # Put side (recoverable — Bucket 2A)
    put_strike: float
    put_expiry: date
    put_premium: float = 0.0
    put_safety_pct: float = 0.0         # how far OTM the strike sits (%)

    # Optional call spread side (defined risk — Bucket 2B)
    call_spread_long_strike: float | None = None
    call_spread_short_strike: float | None = None
    call_spread_expiry: date | None = None
    call_spread_max_loss: float | None = None
    call_spread_premium: float | None = None

    # Combined metrics
    total_premium: float = 0.0
    margin_required: float = 0.0        # from Schwab previewOrder when available
    margin_basis: str = "reg_t"
    roi_margin: float = 0.0             # premium / margin
    roi_cash_secured: float = 0.0       # premium / (strike * 100)

    # Risk metrics
    expected_move: float = 0.0
    put_breach_probability: float = 0.0  # from breach_rate history
    break_even_pct: float = 0.0          # how far stock can fall before a loss


class IVSpikeTrade(BaseModel):
    """An IV-spike (buy premium) trade — NEVER held through earnings.

    Enter 5-10 days before earnings when IV is low; exit 1 day BEFORE the
    announcement to capture IV expansion, not the move. The validator enforces
    ``exit_date < earnings_date`` so a hold-through-earnings setup can never be
    constructed (Phase 3 note 3).
    """

    ticker: str
    strategy: str = IV_SPIKE
    earnings_date: date

    entry_date: date
    exit_date: date                     # hard rule: must be before earnings_date

    instrument: str = "straddle"        # straddle | strangle
    call_strike: float
    put_strike: float
    expiry: date

    total_premium_paid: float = 0.0
    max_loss: float = 0.0               # = total_premium_paid
    target_profit_pct: float = 30.0

    iv_rank_at_entry: int = 0
    iv_expansion_expected: float = 0.0

    @model_validator(mode="after")
    def _exit_before_earnings(self) -> IVSpikeTrade:
        if self.exit_date >= self.earnings_date:
            raise ValueError(
                "IV spike trade must exit BEFORE earnings "
                f"(exit={self.exit_date} >= earnings={self.earnings_date})"
            )
        return self


class EarningsAssessment(BaseModel):
    """LLM (or rule-based fallback) assessment of an earnings trade."""

    recommendation: str = "SKIP"        # EXECUTE | SKIP | REDUCE_SIZE
    confidence: int = Field(default=0, ge=0, le=100)
    probability_of_profit: int = Field(default=0, ge=0, le=100)
    key_risks: list[str] = Field(default_factory=list)
    key_strengths: list[str] = Field(default_factory=list)
    sizing_suggestion: str = "half"     # full | half | quarter
    reasoning: str = ""
    exit_plan: str = ""
    watch_for: str = ""
    model: str = "rule_based"


class EarningsOpportunity(BaseModel):
    """A fully-scored earnings opportunity ready to rank / alert / display."""

    ticker: str
    earnings_date: date
    earnings_time: str = "AMC"
    strategy: str = SKIP                # IV_CRUSH | IV_SPIKE | SKIP
    iv_analysis: IVAnalysis
    trade: IVCrushTrade | IVSpikeTrade | None = None
    llm_assessment: EarningsAssessment | None = None
    overall_score: int = 0              # 0-100
    priority: str = PRIORITY_SKIP       # HIGH | MEDIUM | LOW | SKIP


__all__ = [
    "IV_CRUSH",
    "IV_SPIKE",
    "SKIP",
    "PRIORITY_HIGH",
    "PRIORITY_MEDIUM",
    "PRIORITY_LOW",
    "PRIORITY_SKIP",
    "QuarterlyData",
    "EarningsIVHistory",
    "EarningsEvent",
    "IVAnalysis",
    "IVCrushTrade",
    "IVSpikeTrade",
    "EarningsAssessment",
    "EarningsOpportunity",
]
