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

    def _composite_for(self, ticker: str, data: dict, tdata: dict) -> int:
        """Real composite score from a stock's actual stage data.

        ``data`` carries the fundamental + accumulation fields (from Stages 3-4),
        ``tdata`` carries the technical fields (from the Stage 5 run).
        """
        fund_score = self._scorer.fundamental_score_from_flags(
            eps_accelerating=data.get("eps_accelerating", False),
            rev_reaccelerating=data.get("rev_reaccelerating", False),
            est_revisions_up=data.get("est_revisions_up", False),
        )
        inst_score = self._scorer.institutional_score_from_data(
            holder_count=data.get("holder_count", 0),
            pct_held=data.get("pct_held", 0.0),
            form4_buys=data.get("form4_buys", 0),
        )
        tech_score = self._scorer.technical_score_from_flags(
            is_stage2=tdata.get("is_stage2", False),
            in_base=tdata.get("in_base", False),
            rs_rank=tdata.get("rs_rank", 50.0),
        )
        squeeze_score = self._scorer.squeeze_score_from_short(
            short_ratio=data.get("short_ratio", 0.0),
            short_interest_pct=data.get("short_interest_pct", 0.0),
        )
        tf_score = 100.0 if tdata.get("tf_alignment", False) else 0.0
        return self._scorer.score(
            fundamental_score=fund_score,
            institutional_score=inst_score,
            technical_score=tech_score,
            squeeze_score=squeeze_score,
            tf_alignment_score=tf_score,
        )

    def update_from_stage4(self, stage4_data: dict[str, dict]) -> None:
        """Update watchlist from Stage 3-4 per-stock data — full weekly run.

        Args:
            stage4_data: ``{ticker: {...fundamental + accumulation fields...}}``
                — the merged output of fundamental_screen + accumulation_screen.
                Stage 5 (technical) is run here; its per-stock data is combined
                with ``stage4_data`` to compute each name's real composite score.
        """
        self._init_table()
        stage5 = self._tech_screen.screen(list(stage4_data.keys()))
        stage5_by_ticker = {d["ticker"]: d for d in stage5}

        # Score every Stage 5 survivor; keep only those clearing the threshold.
        qualifying: dict[str, int] = {}
        for ticker, tdata in stage5_by_ticker.items():
            score = self._composite_for(ticker, stage4_data.get(ticker, {}), tdata)
            if self._scorer.passes_watchlist_threshold(score):
                qualifying[ticker] = score
            else:
                logger.debug("Stage5 %s scored %d (< threshold)", ticker, score)

        for ticker, score in qualifying.items():
            self.add(ticker, composite_score=score)  # add or refresh score

        # Remove any active name that no longer qualifies — whether it dropped
        # out of Stage 5 OR is still in Stage 5 but now scores below threshold.
        # (Without this, a name keeps a stale score forever.)
        for entry in self.get_active():
            if entry["ticker"] not in qualifying:
                self.remove(entry["ticker"], reason="below_threshold_or_failed_stage5")

        logger.info(
            "WatchlistManager.update_from_stage4: stage5=%d qualifying=%d active=%d",
            len(stage5_by_ticker), len(qualifying), len(self.get_active()),
        )

    def daily_rescan(self, stage4_data: dict[str, dict]) -> None:
        """Daily rescan: re-screen current Stage 4 survivors for technical changes.

        Args:
            stage4_data: ``{ticker: {...stage 3-4 fields...}}`` (same shape as
                ``update_from_stage4``), so re-scored names keep their real
                fundamental/institutional inputs.
        """
        logger.info("WatchlistManager.daily_rescan: %d Stage 4 tickers", len(stage4_data))
        new_stage5 = self._tech_screen.screen(list(stage4_data.keys()))
        new_by_ticker = {d["ticker"]: d for d in new_stage5}
        self._tech_screen.log_run(len(stage4_data), len(new_by_ticker))

        # Add new breakouts
        existing = {e["ticker"] for e in self.get_active()}
        for ticker, tdata in new_by_ticker.items():
            if ticker not in existing:
                score = self._composite_for(ticker, stage4_data.get(ticker, {}), tdata)
                if self._scorer.passes_watchlist_threshold(score):
                    self.add(ticker, composite_score=score)

        # Remove stocks that broke down
        for entry in self.get_active():
            if entry["ticker"] not in new_by_ticker:
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
