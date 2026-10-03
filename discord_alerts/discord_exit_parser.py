"""
discord_alerts/discord_exit_parser.py

Parses OptionsKit APP profit/loss update messages from the update channel
and auto-closes matched paper trades in the DB.
"""

from __future__ import annotations

import re
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class ExitSignal:
    def __init__(self):
        self.symbol       = None
        self.strike       = None
        self.expiry_short = None
        self.expiry_long  = None
        self.exit_price   = None
        self.pnl_pct      = None
        self.raw_text     = ""

    def __repr__(self):
        return (f"ExitSignal(symbol={self.symbol} strike={self.strike} "
                f"expiry={self.expiry_short} exit_price={self.exit_price} "
                f"pnl={self.pnl_pct}%)")


class ExitParser:
    EXIT_KEYWORDS = [
        "profit", "pt hit", "profit hit", "profit target",
        "profit —", "profit-", "% profit", "roi", "credit to close",
    ]

    def parse(self, text):
        lower = text.lower()
        if not any(k in lower for k in self.EXIT_KEYWORDS):
            return None

        sig = ExitSignal()
        sig.raw_text = text

        ctc = re.search(r'credit\s+to\s+close\s*\n?\s*([\d.]+)', text, re.IGNORECASE)
        if ctc:
            sig.exit_price = float(ctc.group(1))

        roi = re.search(r'ROI\s*\n?\s*[+]?([\d.]+)%', text, re.IGNORECASE)
        if roi:
            sig.pnl_pct = float(roi.group(1))
        else:
            pct = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:profit|pt hit|roi)', text, re.IGNORECASE)
            if pct:
                sig.pnl_pct = float(pct.group(1))

        sym = re.search(
            r'\b(SPX|QQQ|TQQQ|UPRO|TSM|NVDA|AAPL|AMZN|MSFT|AMAT|AVGO|VRT|CRWV|DRAM|SOFI|MCD|MU)\b',
            text, re.IGNORECASE)
        if sym:
            sig.symbol = sym.group(1).upper()

        strike = re.search(r'Strikes?\s*\n?\s*([\d,]+)', text, re.IGNORECASE)
        if strike:
            sig.strike = float(strike.group(1).replace(",", ""))
        else:
            strike2 = re.search(r'\b(\d{3,5}(?:\.\d+)?)\s*(?:CALL|PUT|strike)', text, re.IGNORECASE)
            if strike2:
                sig.strike = float(strike2.group(1))

        exp_block = re.search(
            r'Expir(?:ations?|ies?)\s*\n?\s*(\d{4}-\d{2}-\d{2})\s*/?\s*(\d{4}-\d{2}-\d{2})?',
            text, re.IGNORECASE)
        if exp_block:
            sig.expiry_short = exp_block.group(1)
            sig.expiry_long  = exp_block.group(2)

        if sig.symbol and (sig.exit_price or sig.pnl_pct):
            return sig
        return None


class ExitMatcher:
    def __init__(self, db_path):
        self.db_path = db_path

    def match_and_close(self, exit_sig):
        if not exit_sig.symbol:
            return None

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            candidates = conn.execute(
                "SELECT * FROM discord_signals WHERE paper_status = 'open' AND symbol = ? ORDER BY received_at DESC",
                (exit_sig.symbol,)
            ).fetchall()

            if not candidates:
                log.debug("No open trades for symbol %s", exit_sig.symbol)
                return None

            matched = None

            if exit_sig.strike and exit_sig.expiry_short:
                for c in candidates:
                    if (c["strike"] and abs(c["strike"] - exit_sig.strike) < 1.0
                            and c["expiry_short"] == exit_sig.expiry_short):
                        matched = c
                        break

            if not matched and exit_sig.strike:
                hits = [c for c in candidates
                        if c["strike"] and abs(c["strike"] - exit_sig.strike) < 1.0]
                if hits:
                    if len(hits) > 1:
                        log.info("Multiple open trades for %s strike=%.0f — using most recent",
                                 exit_sig.symbol, exit_sig.strike)
                    matched = hits[0]

            if not matched and len(candidates) == 1:
                matched = candidates[0]

            if not matched:
                log.info("Could not match exit signal to open trade: %s", exit_sig)
                return None

            signal_id = matched["id"]
            entry     = matched["paper_entry"] or 0
            action    = matched["action"] or "BTO"

            exit_price = exit_sig.exit_price
            if not exit_price and exit_sig.pnl_pct and entry:
                if action == "STO":
                    exit_price = entry * (1 - exit_sig.pnl_pct / 100)
                else:
                    exit_price = entry * (1 + exit_sig.pnl_pct / 100)

            if not exit_price:
                log.warning("No exit price derivable for signal #%s", signal_id)
                return None

            if action == "STO":
                pnl_pct = (entry - exit_price) / entry * 100 if entry else None
            else:
                pnl_pct = (exit_price - entry) / entry * 100 if entry else None

            conn.execute(
                """UPDATE discord_signals
                   SET paper_status='closed', paper_exit=?, paper_pnl_pct=?,
                       closed_at=?, skip_reason='auto_exit_from_update'
                   WHERE id=?""",
                (round(exit_price, 4),
                 round(pnl_pct, 2) if pnl_pct is not None else None,
                 datetime.now(timezone.utc).isoformat(),
                 signal_id)
            )

        log.info("✅ Auto-closed trade #%d %s @ %.2f  P&L=%.1f%%",
                 signal_id, exit_sig.symbol, exit_price, pnl_pct or 0)
        return signal_id
