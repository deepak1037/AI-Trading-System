"""Signal contract between all modules (CLAUDE.md Section 5).

These pydantic models are the single source of truth for how signals,
market state, and market snapshots are represented throughout the system.
Every module that produces or consumes a signal must use exactly these types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Direction = Literal["strong_short", "short", "neutral", "long", "strong_long"]
Source = Literal["macro", "yield", "sentiment", "premarket", "technical", "fusion"]
Regime = Literal["strong_short", "short", "neutral", "long", "strong_long"]


class Signal(BaseModel):
    """A single directional signal produced by one signal source."""

    direction: Direction
    confidence: int = Field(ge=0, le=100)
    source: Source
    timestamp: datetime
    metadata: dict = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"confidence must be 0-100, got {v}")
        return v


class MarketState(BaseModel):
    """Aggregate regime tracker. Alerts fire only on regime TRANSITION."""

    current_regime: Regime
    composite_score: int = Field(ge=0, le=100)
    last_updated: datetime
    signals_active: list[Signal] = Field(default_factory=list)
    previous_regime: Optional[Regime] = None

    @property
    def regime_changed(self) -> bool:
        return self.previous_regime != self.current_regime

    @property
    def direction(self) -> Regime:
        """Alias for current_regime (same Direction literal values)."""
        return self.current_regime

    @property
    def confidence(self) -> int:
        """Mean confidence of the active source signals (0 if none)."""
        if not self.signals_active:
            return 0
        return round(sum(s.confidence for s in self.signals_active) / len(self.signals_active))


class MarketSnapshot(BaseModel):
    """Real-time market data snapshot for one ticker."""

    timestamp: datetime
    ticker: str
    price: float
    volume: int
    vwap: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None


__all__ = [
    "Direction",
    "Source",
    "Regime",
    "Signal",
    "MarketState",
    "MarketSnapshot",
]
