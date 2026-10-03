"""discord_alerts/discord_paper_bridge.py

Bridges Discord signal trades to the PaperAccount.

When the discord listener opens or closes a trade in the discord_signals
SQLite table, this bridge mirrors that action to the paper_discord
PaperAccount (JSON file), so the $100k balance actually reflects open
positions and P&L.

Usage — call from discord_user_listener.py after a trade is saved/closed:

    bridge = DiscordPaperBridge()

    # On entry signal:
    bridge.open_trade(signal)

    # On exit (from _close_trade in exit_rules):
    bridge.close_trade(trade_id, exit_price, pnl_pct, reason)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from broker_core.account_registry import get_paper_account
from broker_core.base_broker import Order, OrderResult
from config.settings import settings

log = logging.getLogger(__name__)

# One options contract = 100 shares * price
_OPTIONS_MULTIPLIER = 100


class DiscordPaperBridge:
    """Mirrors discord trade signals into the paper_discord PaperAccount."""

    def __init__(self, account_id: Optional[str] = None) -> None:
        acct_id = account_id or settings.DISCORD_PAPER_ACCOUNT
        self._account = get_paper_account(acct_id)
        log.info("DiscordPaperBridge: using account=%s", acct_id)

    # ── Public API ────────────────────────────────────────────────────────────

    def open_trade(self, signal) -> bool:
        """Record an entry signal as an open position in PaperAccount.

        ``signal`` is a ParsedSignal dataclass from discord_signal_reader.
        Returns True if applied, False if skipped (no price, etc.).
        """
        entry_price = getattr(signal, "paper_entry", None) or getattr(signal, "limit_price", None)
        if not entry_price:
            log.warning("DiscordPaperBridge: no entry price for %s — skipping", getattr(signal, "symbol", "?"))
            return False

        contracts = getattr(signal, "contracts", 1) or 1
        symbol    = getattr(signal, "symbol", "UNKNOWN") or "UNKNOWN"
        action    = getattr(signal, "action", "BTO") or "BTO"
        strategy  = getattr(signal, "strategy_type", "discord") or "discord"

        # Cost = entry_price * contracts * 100 (options multiplier)
        cost = entry_price * contracts * _OPTIONS_MULTIPLIER

        state = self._account.get_state()
        if state.cash < cost:
            log.warning(
                "DiscordPaperBridge: insufficient cash (have $%.2f, need $%.2f) for %s — skipping",
                state.cash, cost, symbol,
            )
            return False

        # Build a synthetic equity Order so PaperAccount.apply_fill() works.
        # We treat contracts*100 as "qty" at per-share price so the math
        # (fill_price * qty = total cost) stays correct.
        order = Order(
            ticker       = symbol,
            action       = "BUY" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "SELL",
            qty          = contracts * _OPTIONS_MULTIPLIER,
            order_type   = "limit",
            limit_price  = entry_price,
            strategy_name= f"discord_{strategy}",
            account_id   = self._account.account_id,
        )
        self._account.apply_fill(order, fill_price=entry_price, commission=0.0)

        log.info(
            "DiscordPaperBridge: OPEN %s %s  contracts=%d  entry=%.4f  cost=$%.2f",
            action, symbol, contracts, entry_price, cost,
        )
        return True

    def close_trade(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        contracts: int,
        action: str,
        reason: str = "exit",
    ) -> bool:
        """Record a trade close in PaperAccount.

        The action is the ENTRY action (BTO/STO); the bridge derives the
        closing action automatically.
        """
        if not exit_price or not entry_price:
            log.warning("DiscordPaperBridge: missing price for close %s — skipping", symbol)
            return False

        contracts = contracts or 1
        close_action = "SELL" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "BUY"

        order = Order(
            ticker       = symbol,
            action       = close_action,  # type: ignore[arg-type]
            qty          = contracts * _OPTIONS_MULTIPLIER,
            order_type   = "market",
            strategy_name= f"discord_{reason}",
            account_id   = self._account.account_id,
        )
        self._account.apply_fill(order, fill_price=exit_price, commission=0.0)

        pnl = (exit_price - entry_price) * contracts * _OPTIONS_MULTIPLIER
        if action in ("STO", "SELL", "SELL_TO_OPEN"):
            pnl = -pnl  # STO profits when price goes down

        log.info(
            "DiscordPaperBridge: CLOSE %s  contracts=%d  entry=%.4f  exit=%.4f  P&L=$%.2f  reason=%s",
            symbol, contracts, entry_price, exit_price, pnl, reason,
        )
        return True

    def get_summary(self) -> dict:
        """Return a quick snapshot of the paper account state."""
        state = self._account.get_state()
        perf  = self._account.get_performance()
        return {
            "account_id"      : state.account_id,
            "cash"            : round(state.cash, 2),
            "equity"          : round(state.equity, 2),
            "open_positions"  : len(state.open_positions),
            "closed_positions": len(state.closed_positions),
            "total_return_pct": round(perf.total_return_pct, 2),
        }


__all__ = ["DiscordPaperBridge"]
