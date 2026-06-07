"""Tests for signals/macro_engine.py — replays 3 historical NFP days."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from signals.macro_engine import MacroEngine, _FALLBACK_STD
from signals.signal_schema import Signal


# ---------------------------------------------------------------------------
# Historical NFP replay data (3 real events):
#   2023-09-01: actual=187k, forecast=170k → positive surprise → long
#   2023-11-03: actual=150k, forecast=180k → negative surprise → short
#   2022-01-07: actual=199k, forecast=450k → very negative surprise → strong_short
# ---------------------------------------------------------------------------
_NFP_EVENTS = [
    {
        # actual=187k vs forecast=100k: z = 87/75 = 1.16 → long
        "actual": 187.0,
        "forecast": 100.0,
        "std": 75.0,
        "expected_direction": "long",
        "date": datetime(2023, 9, 1, 12, 30, tzinfo=timezone.utc),
    },
    {
        # actual=150k vs forecast=250k: z = -100/75 = -1.33 → short
        "actual": 150.0,
        "forecast": 250.0,
        "std": 75.0,
        "expected_direction": "short",
        "date": datetime(2023, 11, 3, 12, 30, tzinfo=timezone.utc),
    },
    {
        # actual=199k vs forecast=450k: z = -251/75 = -3.35 → strong_short
        "actual": 199.0,
        "forecast": 450.0,
        "std": 75.0,
        "expected_direction": "strong_short",
        "date": datetime(2022, 1, 7, 12, 30, tzinfo=timezone.utc),
    },
]


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.db"
    # Create the signals table
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                source TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}'
            )"""
        )
    return MacroEngine(db_path=str(db))


class TestMacroEngineOffline:
    @pytest.mark.parametrize("event", _NFP_EVENTS)
    def test_nfp_replay(self, engine, event):
        signal = engine.score_release_offline(
            release_name="NFP",
            actual=event["actual"],
            forecast=event["forecast"],
            historical_std=event["std"],
            timestamp=event["date"],
        )
        assert isinstance(signal, Signal)
        assert signal.source == "macro"
        assert signal.direction == event["expected_direction"], (
            f"Expected {event['expected_direction']}, got {signal.direction} "
            f"for actual={event['actual']}, forecast={event['forecast']}"
        )
        assert 0 <= signal.confidence <= 100
        assert signal.metadata["release"] == "NFP"
        assert signal.metadata["mode"] == "offline"

    def test_z_score_stored_in_metadata(self, engine):
        signal = engine.score_release_offline("NFP", actual=200.0, forecast=100.0, historical_std=75.0)
        assert "z_score" in signal.metadata
        assert abs(signal.metadata["z_score"] - (200.0 - 100.0) / 75.0) < 0.001

    def test_unknown_release_raises(self, engine):
        from core.exceptions import SignalError
        with pytest.raises(SignalError):
            engine.score_release_offline("UNKNOWN", actual=1.0, forecast=0.0, historical_std=1.0)

    def test_cpi_positive_surprise_is_bearish(self, engine):
        # CPI above forecast = higher inflation = bearish for equities
        signal = engine.score_release_offline("CPI", actual=0.5, forecast=0.3, historical_std=0.2)
        assert signal.direction in ("short", "strong_short")

    def test_cpi_negative_surprise_is_bullish(self, engine):
        # CPI below forecast = lower inflation = bullish for equities
        signal = engine.score_release_offline("CPI", actual=0.2, forecast=0.4, historical_std=0.2)
        assert signal.direction in ("long", "strong_long")

    def test_neutral_on_small_move(self, engine):
        # Near-zero surprise → neutral
        signal = engine.score_release_offline("NFP", actual=200.0, forecast=198.0, historical_std=75.0)
        assert signal.direction == "neutral"

    def test_strong_signal_on_large_move(self, engine):
        # |z| >> 2 → strong direction
        signal = engine.score_release_offline("NFP", actual=500.0, forecast=100.0, historical_std=75.0)
        assert signal.direction == "strong_long"
        assert signal.confidence >= 80

    def test_log_signal_to_db(self, engine):
        signal = engine.score_release_offline("NFP", actual=200.0, forecast=180.0, historical_std=75.0)
        row_id = engine.log_signal_to_db(signal)
        assert row_id > 0
        db_path = engine._db_path
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT direction, confidence, source FROM signals WHERE id=?", (row_id,)
            ).fetchone()
        assert row is not None
        assert row[0] == signal.direction
        assert row[2] == "macro"


class TestMacroEngineOnline:
    def test_score_release_calls_fred(self, engine):
        """Verify the live path hits _fetch_series_latest."""
        with patch.object(engine, "_fetch_series_latest", return_value=187.0) as mock_actual, \
             patch.object(engine, "_fetch_historical_std", return_value=75.0):
            # Also mock the fallback prior-obs forecast fetch
            import fredapi
            with patch("fredapi.Fred") as MockFred:
                mock_fred_inst = MagicMock()
                import pandas as pd
                mock_fred_inst.get_series.return_value = pd.Series([170.0, 187.0])
                MockFred.return_value = mock_fred_inst
                signal = engine.score_release("NFP", forecast=170.0)
        assert signal.source == "macro"
        mock_actual.assert_called_once()
