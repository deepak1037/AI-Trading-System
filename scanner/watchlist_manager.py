"""Watchlist manager — self-updating watchlist from scanner pipeline (Days 13-18).

Self-update logic (CLAUDE.md Section 14):
  - Weekly: full 5-stage funnel rerun (Sunday)
  - Daily: technical + options rescan on current ~120 Stage 4 survivors only
  - Real-time: options flow threshold crossed → instant watchlist promotion
  - Auto-remove: stock breaks below key technical level
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger
from scanner.scorer import CompositeScorer
from scanner.technical_screen import TechnicalScreen

logger = get_logger(__name__)


class WatchlistManager:
    """Manages the active watchlist — add/remove/rescan tickers."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._scorer = CompositeScorer()
        self._tech_screen = TechnicalScreen(db_path=self._db_path)

    def _init_table(self) -> None:
        """Ensure watchlist table exists."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    name TEXT,
                    composite_score INTEGER,
                    short_tf INTEGER DEFAULT 0,
                    mid_tf INTEGER DEFAULT 0,
                    long_tf INTEGER DEFAULT 0,
                    added_at TEXT NOT NULL,
                    removed_at TEXT,
                    is_active INTEGER DEFAULT 1
                )
            """)

    def add(self, ticker: str, composite_score: int, name: str = "") -> None:
        """Add or re-activate a ticker on the watchlist."""
        self._init_table()
        now = datetime.now(tz=timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO watchlist (ticker, name, composite_score, added_at, is_active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(ticker) DO UPDATE SET
                    composite_score=excluded.composite_score,
                    added_at=excluded.added_at,
                    removed_at=NULL,
                    is_active=1
            """, (ticker, name, composite_score, now))
        logger.info("Watchlist: added %s score=%d", ticker, composite_score)

    def remove(self, ticker: str, reason: str = "") -> None:
        """Deactivate a ticker from the watchlist."""
        self._init_table()
        now = datetime.now(tz=timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE watchlist SET is_active=0, removed_at=? WHERE ticker=?",
                (now, ticker),
            )
        logger.info("Watchlist: removed %s reason=%s", ticker, reason)

    def get_active(self) -> list[dict]:
        """Return all active watchlist entries."""
        self._init_table()
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM watchlist WHERE is_active=1 ORDER BY composite_score DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_from_stage4(self, stage4_tickers: list[str]) -> None:
        """Update watchlist from Stage 4 survivors — full weekly run."""
        self._init_table()
        stage5 = self._tech_screen.screen(stage4_tickers)

        added = 0
        for ticker in stage5:
            # Compute composite score with default weights
            score = self._scorer.score(
                fundamental_score=70.0,
                institutional_score=70.0,
                technical_score=80.0,
                squeeze_score=50.0,
                tf_alignment_score=60.0,
            )
            if self._scorer.passes_watchlist_threshold(score):
                self.add(ticker, composite_score=score)
                added += 1

        # Auto-remove tickers that no longer pass Stage 5
        active = self.get_active()
        for entry in active:
            if entry["ticker"] not in stage5:
                self.remove(entry["ticker"], reason="failed_stage5_rescan")

        logger.info(
            "WatchlistManager.update_from_stage4: added=%d removed=%d active=%d",
            added,
            len(active) - added,
            len(self.get_active()),
        )

    def daily_rescan(self, stage4_survivors: list[str]) -> None:
        """Daily rescan: only rescan current Stage 4 survivors for technical changes."""
        logger.info("WatchlistManager.daily_rescan: %d Stage 4 tickers", len(stage4_survivors))
        new_stage5 = self._tech_screen.screen(stage4_survivors)
        self._tech_screen.log_run(len(stage4_survivors), len(new_stage5))

        # Add new breakouts
        existing = {e["ticker"] for e in self.get_active()}
        for ticker in new_stage5:
            if ticker not in existing:
                score = self._scorer.score(technical_score=80.0, tf_alignment_score=60.0)
                if self._scorer.passes_watchlist_threshold(score):
                    self.add(ticker, composite_score=score)

        # Remove stocks that broke down
        for entry in self.get_active():
            if entry["ticker"] not in new_stage5:
                self.remove(entry["ticker"], reason="technical_breakdown")

    def promote_on_options_flow(self, ticker: str, options_score: float) -> None:
        """Instant watchlist promotion when options flow threshold crossed.

        High unusual options flow signals institutional conviction across multiple
        dimensions — score it broadly rather than just the technical weight.
        """
        # Options flow above 80 = very strong signal — score all dimensions proportionally
        score = self._scorer.score(
            fundamental_score=options_score,
            institutional_score=options_score,
            technical_score=options_score,
            squeeze_score=options_score * 0.5,
            tf_alignment_score=options_score,
        )
        if score >= settings.SCANNER_WATCHLIST_THRESHOLD:
            self.add(ticker, composite_score=score)
            logger.info("Watchlist: instant promotion %s via options flow score=%.1f", ticker, options_score)


__all__ = ["WatchlistManager"]
