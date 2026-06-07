"""Tests for signals/signal_schema.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from signals.signal_schema import MarketSnapshot, MarketState, Signal


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class TestSignal:
    def test_valid_signal(self):
        s = Signal(
            direction="long",
            confidence=75,
            source="macro",
            timestamp=_now(),
        )
        assert s.direction == "long"
        assert s.confidence == 75
        assert s.source == "macro"
        assert s.metadata == {}

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            Signal(direction="long", confidence=101, source="macro", timestamp=_now())
        with pytest.raises(ValidationError):
            Signal(direction="long", confidence=-1, source="macro", timestamp=_now())

    def test_invalid_direction(self):
        with pytest.raises(ValidationError):
            Signal(direction="up", confidence=50, source="macro", timestamp=_now())

    def test_invalid_source(self):
        with pytest.raises(ValidationError):
            Signal(direction="long", confidence=50, source="unknown", timestamp=_now())

    def test_metadata_stored(self):
        s = Signal(
            direction="short",
            confidence=60,
            source="yield",
            timestamp=_now(),
            metadata={"z_score": 1.5},
        )
        assert s.metadata["z_score"] == 1.5


class TestMarketState:
    def test_regime_changed_true(self):
        ms = MarketState(
            current_regime="long",
            composite_score=70,
            last_updated=_now(),
            previous_regime="neutral",
        )
        assert ms.regime_changed is True

    def test_regime_changed_false(self):
        ms = MarketState(
            current_regime="neutral",
            composite_score=50,
            last_updated=_now(),
            previous_regime="neutral",
        )
        assert ms.regime_changed is False

    def test_no_previous_regime(self):
        ms = MarketState(
            current_regime="long",
            composite_score=70,
            last_updated=_now(),
        )
        assert ms.previous_regime is None
        assert ms.regime_changed is True


class TestMarketSnapshot:
    def test_valid_snapshot(self):
        snap = MarketSnapshot(
            timestamp=_now(),
            ticker="AAPL",
            price=175.50,
            volume=1_000_000,
        )
        assert snap.ticker == "AAPL"
        assert snap.vwap is None
        assert snap.bid is None

    def test_optional_fields(self):
        snap = MarketSnapshot(
            timestamp=_now(),
            ticker="SPY",
            price=500.0,
            volume=5_000_000,
            vwap=499.50,
            bid=499.90,
            ask=500.10,
        )
        assert snap.vwap == 499.50
        assert snap.bid == 499.90
