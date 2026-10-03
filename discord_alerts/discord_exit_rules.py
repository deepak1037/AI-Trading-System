"""
discord_alerts/discord_exit_rules.py

Rule engine for automated exit decisions.
Loads exit_rules.yaml and applies PT / DTE / Ravish-signal logic
to every profit update received from the update channel or income-trades forum.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

import yaml

from discord_alerts.discord_paper_bridge import DiscordPaperBridge

log = logging.getLogger(__name__)

RAVISH_EXIT_KEYWORDS = [
    "all out", "all-out", "closed", "expired", "closing",
    "take profit", "took profit", "exit", "exiting",
    "100% profit", "100%", "max profit",
]

def _detect_strategy(row: dict) -> str:
    action        = (row.get("action") or "").upper()
    strategy_type = (row.get("strategy_type") or "").lower()
    symbol        = (row.get("symbol") or "").upper()
    expiry_short  = row.get("expiry_short") or ""
    expiry_long   = row.get("expiry_long") or ""
    if action == "BTO" and expiry_short and expiry_long and symbol == "SPX":
        return "call_calendar"
    if strategy_type in ("call_spread", "put_spread"):
        return strategy_type
    if "spread" in strategy_type:
        return "spread"
    return strategy_type or "unknown"

def _days_to_expiry(expiry_str: Optional[str]) -> Optional[int]:
    if not expiry_str:
        return None
    try:
        exp = date.fromisoformat(expiry_str[:10])
        return (exp - date.today()).days
    except ValueError:
        return None

def _dte_at_entry(received_at: Optional[str], expiry_str: Optional[str]) -> Optional[int]:
    if not expiry_str or not received_at:
        return None
    try:
        exp   = date.fromisoformat(expiry_str[:10])
        entry = date.fromisoformat(received_at[:10])
        return (exp - entry).days
    except ValueError:
        return None


class ExitRuleEngine:
    def __init__(self, rules_path: str, db_path: str, notifier=None):
        self.rules_path = rules_path
        self.db_path    = db_path
        self.notifier   = notifier
        self.rules      = {}
        self.sizing     = {}
        self._bridge    = DiscordPaperBridge()
        self._load()

    def _load(self):
        if not os.path.exists(self.rules_path):
            log.warning("exit_rules.yaml not found at %s — using defaults", self.rules_path)
            return
        with open(self.rules_path) as f:
            data = yaml.safe_load(f)
        self.sizing = data.get("position_sizing", {})
        self.rules  = data.get("rules", {})
        log.info("Loaded %d exit rules from %s", len(self.rules), self.rules_path)

    def reload(self):
        self._load()

    def _match_rule(self, row: dict) -> tuple:
        action        = (row.get("action") or "").upper()
        symbol        = (row.get("symbol") or "").upper()
        strategy_type = _detect_strategy(row)
        dte_at_entry  = _dte_at_entry(row.get("received_at"), row.get("expiry_short"))

        for name, rule in self.rules.items():
            if name == "default":
                continue
            m = rule.get("match", {})
            if "action" in m and m["action"].upper() != action:
                continue
            if "symbol" in m and m["symbol"].upper() != symbol:
                continue
            if "symbols" in m and symbol not in [s.upper() for s in m["symbols"]]:
                continue
            if "strategy_type" in m:
                st = m["strategy_type"]
                if isinstance(st, list):
                    if strategy_type not in [s.lower() for s in st]:
                        continue
                elif strategy_type != st.lower():
                    continue
            if "min_dte_at_entry" in m:
                if dte_at_entry is None or dte_at_entry < m["min_dte_at_entry"]:
                    continue
            return name, rule

        return "default", self.rules.get("default", {
            "profit_target_pct": 50,
            "dte_stop": 14,
            "auto_close_at_dte": False,
            "auto_close_at_pt": False,
            "auto_close_ravish": True,
        })

    def is_ravish_exit(self, text: str) -> Optional[str]:
        lower = text.lower()
        for kw in RAVISH_EXIT_KEYWORDS:
            if kw in lower:
                return kw
        return None

    async def validate_new_trade(self, signal):
        if not self.notifier:
            return
        row = {
            "action":        getattr(signal, "action", None),
            "symbol":        getattr(signal, "symbol", None),
            "strategy_type": getattr(signal, "strategy_type", None),
            "expiry_short":  getattr(signal, "expiry_short", None),
            "expiry_long":   getattr(signal, "expiry_long", None),
            "received_at":   datetime.now(timezone.utc).isoformat(),
            "id":            getattr(signal, "id", 0),
        }
        rule_name, rule = self._match_rule(row)
        if rule_name == "default":
            await self.notifier.alert_no_rule(
                symbol        = row["symbol"] or "?",
                action        = row["action"] or "?",
                strategy_type = row["strategy_type"] or "unknown",
                trade_id      = row["id"],
            )
            return
        log.info("Exit rule matched: %s (PT=%s%% DTE<=%s) for %s %s",
                 rule_name, rule.get("profit_target_pct"), rule.get("dte_stop"),
                 row["action"], row["symbol"])
        max_pos = self.sizing.get("max_concurrent_positions", 8)
        with sqlite3.connect(self.db_path) as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM discord_signals WHERE paper_status='open'"
            ).fetchone()[0]
        if open_count > max_pos:
            await self.notifier.alert_position_limit(
                symbol=row["symbol"] or "?", open_count=open_count, max_count=max_pos)

    async def evaluate(self, trade_row: dict, pnl_pct: Optional[float],
                       exit_price: Optional[float], message_text: str) -> bool:
        trade_id = trade_row["id"]
        symbol   = trade_row.get("symbol") or "?"
        action   = trade_row.get("action") or "?"
        rule_name, rule = self._match_rule(trade_row)
        dte = _days_to_expiry(trade_row.get("expiry_short"))

        ravish_trigger = self.is_ravish_exit(message_text)
        if ravish_trigger and rule.get("auto_close_ravish", True):
            log.info("Ravish exit detected ('%s') closing trade #%d", ravish_trigger, trade_id)
            closed = self._close_trade(trade_id, exit_price, pnl_pct, "ravish_exit")
            if closed and self.notifier:
                await self.notifier.alert_ravish_exit(symbol, pnl_pct, trade_id, ravish_trigger)
            return closed

        dte_stop = rule.get("dte_stop")
        if dte is not None and dte_stop is not None and dte <= dte_stop:
            auto = rule.get("auto_close_at_dte", True)
            log.info("DTE stop: %s %s DTE=%d <= %d (auto=%s)", action, symbol, dte, dte_stop, auto)
            if self.notifier:
                await self.notifier.alert_dte_stop(symbol, action, pnl_pct, dte, trade_id, auto)
            if auto:
                return self._close_trade(trade_id, exit_price, pnl_pct, "dte_stop")
            return False

        pt = rule.get("profit_target_pct")
        if pnl_pct is not None and pt is not None and pnl_pct >= pt:
            auto = rule.get("auto_close_at_pt", False)
            log.info("PT hit: %s %s pnl=%.1f%% >= %.0f%% (auto=%s)", action, symbol, pnl_pct, pt, auto)
            if self.notifier:
                await self.notifier.alert_pt_hit(symbol, action, pnl_pct, pt, dte, trade_id)
            if auto:
                return self._close_trade(trade_id, exit_price, pnl_pct, "profit_target")
            return False

        log.info("Holding %s %s  pnl=%s%%  DTE=%s  rule=%s",
                 action, symbol,
                 f"{pnl_pct:.1f}" if pnl_pct is not None else "?",
                 dte if dte is not None else "?", rule_name)
        return False

    def _close_trade(self, trade_id: int, exit_price: Optional[float],
                     pnl_pct: Optional[float], reason: str) -> bool:
        if not exit_price and not pnl_pct:
            log.warning("Cannot close trade #%d — no exit price or P&L", trade_id)
            return False
        if not exit_price:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT paper_entry, action FROM discord_signals WHERE id=?", (trade_id,)
                ).fetchone()
                if row and row["paper_entry"]:
                    entry = row["paper_entry"]
                    action = row["action"] or "BTO"
                    if action == "STO":
                        exit_price = entry * (1 - pnl_pct / 100)
                    else:
                        exit_price = entry * (1 + pnl_pct / 100)
        if not exit_price:
            log.warning("Cannot derive exit price for trade #%d", trade_id)
            return False
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT paper_entry, action, contracts, symbol FROM discord_signals WHERE id=?",
                (trade_id,)
            ).fetchone()
            if not row:
                return False
            entry     = row["paper_entry"] or 0
            action    = row["action"] or "BTO"
            contracts = row["contracts"] or 1
            symbol    = row["symbol"] or "?"
            if entry:
                if action == "STO":
                    computed_pnl = (entry - exit_price) / entry * 100
                else:
                    computed_pnl = (exit_price - entry) / entry * 100
            else:
                computed_pnl = pnl_pct
            conn.execute(
                """UPDATE discord_signals
                   SET paper_status='closed', paper_exit=?, paper_pnl_pct=?,
                       closed_at=?, skip_reason=?
                   WHERE id=?""",
                (round(exit_price, 4),
                 round(computed_pnl, 2) if computed_pnl is not None else None,
                 datetime.now(timezone.utc).isoformat(), reason, trade_id)
            )
        log.info("Closed trade #%d @ %.2f  P&L=%.1f%%  reason=%s",
                 trade_id, exit_price, computed_pnl or 0, reason)

        # Mirror close to the paper_discord PaperAccount
        self._bridge.close_trade(
            symbol      = symbol,
            entry_price = entry,
            exit_price  = exit_price,
            contracts   = contracts,
            action      = action,
            reason      = reason,
        )
        return True
