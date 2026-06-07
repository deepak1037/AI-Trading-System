"""Tests for scanner/watchlist_manager.py (Days 13-18)."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


from scanner.watchlist_manager import WatchlistManager


def _init_db(db_path: str) -> None:
    """Create the scanner_runs table needed by scanner modules."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scanner_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage INTEGER NOT NULL,
                tickers_in INTEGER,
                tickers_out INTEGER,
                run_at TEXT NOT NULL
            )
        """)


class TestWatchlistManager:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "test.db")
        _init_db(self.db_path)
        self.wm = WatchlistManager(db_path=self.db_path)

    def test_add_ticker(self):
        self.wm.add("AAPL", composite_score=75)
        active = self.wm.get_active()
        assert any(e["ticker"] == "AAPL" for e in active)

    def test_remove_ticker(self):
        self.wm.add("MSFT", composite_score=70)
        self.wm.remove("MSFT", reason="test")
        active = self.wm.get_active()
        assert not any(e["ticker"] == "MSFT" for e in active)

    def test_re_add_removed_ticker(self):
        self.wm.add("GOOG", composite_score=72)
        self.wm.remove("GOOG")
        self.wm.add("GOOG", composite_score=80)  # should re-activate
        active = self.wm.get_active()
        assert any(e["ticker"] == "GOOG" for e in active)

    def test_get_active_empty(self):
        active = self.wm.get_active()
        assert active == []

    def test_get_active_ordered_by_score(self):
        self.wm.add("A", composite_score=60)
        self.wm.add("B", composite_score=90)
        self.wm.add("C", composite_score=75)
        active = self.wm.get_active()
        scores = [e["composite_score"] for e in active]
        assert scores == sorted(scores, reverse=True)

    def test_promote_on_options_flow_above_threshold(self):
        self.wm.promote_on_options_flow("NVDA", options_score=90.0)
        active = self.wm.get_active()
        assert any(e["ticker"] == "NVDA" for e in active)

    def test_promote_on_options_flow_below_threshold(self):
        self.wm.promote_on_options_flow("XYZ", options_score=10.0)
        active = self.wm.get_active()
        assert not any(e["ticker"] == "XYZ" for e in active)

    def test_add_duplicate_updates_score(self):
        self.wm.add("AMD", composite_score=70)
        self.wm.add("AMD", composite_score=85)
        active = self.wm.get_active()
        amd_entry = next(e for e in active if e["ticker"] == "AMD")
        assert amd_entry["composite_score"] == 85
