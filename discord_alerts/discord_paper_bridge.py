"""discord_alerts/discord_paper_bridge.py

Bridges Discord signal trades to the PaperAccount using Schwab previewOrder.

When a tradeable Discord signal arrives, this bridge:
  1. Builds an OptionsOrder from the ParsedSignal fields
  2. Calls SchwabBroker.preview_options_order() — real Schwab API call, no money moves
  3. Gets back real margin/buying-power impact from Schwab
  4. Applies that cost to the paper_discord PaperAccount via apply_preview()
  5. Checks a configurable buying-power buffer before opening each trade

This means the paper account reflects realistic margin usage, not just
"limit_price × contracts × 100".

Usage — called from discord_user_listener.py after a signal is saved:

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

from broker_core.account_registry import get_broker_for, get_paper_account
from broker_core.base_broker import OptionsOrder, Order, OrderPreview
from config.settings import settings

log = logging.getLogger(__name__)

# One options contract = 100 shares × price
_OPTIONS_MULTIPLIER = 100

# Minimum projected available funds after a trade before we skip it.
# Override via DISCORD_PAPER_MIN_BUFFER in .env (default $5,000).
_DEFAULT_BUFFER = 5_000.0


def _build_occ_symbol(symbol: str, expiry: str, option_type: str, strike: float) -> str:
    """Build an OCC option symbol from ParsedSignal components.

    OCC format: <symbol padded to 6><YYMMDD><C/P><strike × 1000 zero-padded to 8>
    Example: SPX   251219C05500000
    """
    sym    = symbol.upper().ljust(6)
    # expiry_short is stored as YYYY-MM-DD
    try:
        dt  = datetime.strptime(expiry[:10], "%Y-%m-%d")
        ymd = dt.strftime("%y%m%d")
    except (ValueError, TypeError):
        ymd = "000000"
    cp     = "C" if option_type.upper().startswith("C") else "P"
    strike_int = int(round(strike * 1000))
    return f"{sym}{ymd}{cp}{strike_int:08d}"


class DiscordPaperBridge:
    """Mirrors Discord trade signals into the paper_discord PaperAccount.

    Uses Schwab previewOrder to get real margin/buying-power impact before
    booking each trade, so the $100k balance reflects realistic cost.
    """

    def __init__(self, account_id: Optional[str] = None) -> None:
        acct_id         = account_id or settings.DISCORD_PAPER_ACCOUNT
        self._account   = get_paper_account(acct_id)
        self._broker    = get_broker_for(settings.DISCORD_SCHWAB_ACCOUNT or None)
        self._min_buffer = float(getattr(settings, "DISCORD_PAPER_MIN_BUFFER", _DEFAULT_BUFFER))
        log.info(
            "DiscordPaperBridge: account=%s  broker=%s  min_buffer=$%.0f",
            acct_id, type(self._broker).__name__, self._min_buffer,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def open_trade(self, signal) -> bool:
        """Record an entry signal as an open position via Schwab previewOrder.

        ``signal`` is a ParsedSignal dataclass from discord_signal_reader.
        Returns True if applied, False if skipped.
        """
        entry_price = getattr(signal, "paper_entry", None) or getattr(signal, "limit_price", None)
        if not entry_price:
            log.warning(
                "DiscordPaperBridge: no entry price for %s — skipping",
                getattr(signal, "symbol", "?"),
            )
            return False

        symbol      = (getattr(signal, "symbol",       None) or "UNKNOWN").upper()
        action      = (getattr(signal, "action",       None) or "BTO").upper()
        contracts   = getattr(signal, "contracts",     1) or 1
        strategy    = getattr(signal, "strategy_type", None) or "discord"
        strike      = getattr(signal, "strike",        None)
        expiry      = getattr(signal, "expiry_short",  None)
        option_type = getattr(signal, "option_type",   None)

        # ── Try Schwab preview if we have enough info for an OCC symbol ──────
        preview = self._schwab_preview(
            symbol=symbol, action=action, contracts=contracts,
            limit_price=entry_price, strike=strike,
            expiry=expiry, option_type=option_type,
            strategy=strategy,
        )

        if preview is not None:
            # Real Schwab cost — check buying-power buffer
            if not preview.is_valid:
                log.warning(
                    "DiscordPaperBridge: Schwab rejected preview for %s — %s",
                    symbol, preview.rejection_reason,
                )
                return False

            if preview.projected_available_fund < self._min_buffer:
                log.warning(
                    "DiscordPaperBridge: insufficient buying power after trade "
                    "(projected_available=$%.2f < buffer=$%.2f) for %s — skipping",
                    preview.projected_available_fund, self._min_buffer, symbol,
                )
                return False

            # Build a synthetic Order so apply_preview() math works:
            # apply_preview does fill_price = estimated_cost / qty
            # We pass qty = contracts (not ×100) because estimated_cost is
            # already the total order value from Schwab.
            order = Order(
                ticker        = symbol,
                action        = "BUY" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "SELL",
                qty           = contracts,
                order_type    = "limit",
                limit_price   = entry_price,
                strategy_name = f"discord_{strategy}",
                account_id    = self._account.account_id,
            )
            result = self._account.apply_preview(order, preview)
            log.info(
                "DiscordPaperBridge: OPEN %s %s  contracts=%d  "
                "schwab_cost=$%.2f  buying_power_left=$%.2f  status=%s",
                action, symbol, contracts,
                preview.estimated_cost, preview.projected_available_fund,
                result.status,
            )
            return result.status == "filled"

        # ── Fallback: no OCC symbol → use signal price directly ──────────────
        log.info(
            "DiscordPaperBridge: no OCC symbol for %s (strike=%s expiry=%s) "
            "— falling back to limit_price fill",
            symbol, strike, expiry,
        )
        cost = entry_price * contracts * _OPTIONS_MULTIPLIER
        state = self._account.get_state()
        if state.cash < cost:
            log.warning(
                "DiscordPaperBridge: insufficient cash (have $%.2f, need $%.2f) for %s — skipping",
                state.cash, cost, symbol,
            )
            return False

        order = Order(
            ticker        = symbol,
            action        = "BUY" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "SELL",
            qty           = contracts * _OPTIONS_MULTIPLIER,
            order_type    = "limit",
            limit_price   = entry_price,
            strategy_name = f"discord_{strategy}",
            account_id    = self._account.account_id,
        )
        self._account.apply_fill(order, fill_price=entry_price, commission=0.0)
        log.info(
            "DiscordPaperBridge: OPEN %s %s  contracts=%d  entry=%.4f  cost=$%.2f  (fallback)",
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

        contracts    = contracts or 1
        close_action = "SELL" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "BUY"

        # Close with qty=contracts*100 to match the fallback open path.
        # For Schwab-preview opens, qty was set to contracts (not ×100),
        # so PaperAccount FIFO matching works either way — it matches by ticker.
        order = Order(
            ticker        = symbol,
            action        = close_action,  # type: ignore[arg-type]
            qty           = contracts * _OPTIONS_MULTIPLIER,
            order_type    = "market",
            strategy_name = f"discord_{reason}",
            account_id    = self._account.account_id,
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

    # ── Internal ──────────────────────────────────────────────────────────────

    def _schwab_preview(
        self,
        symbol: str,
        action: str,
        contracts: int,
        limit_price: float,
        strike: Optional[float],
        expiry: Optional[str],
        option_type: Optional[str],
        strategy: str,
    ) -> Optional[OrderPreview]:
        """Call Schwab previewOrder for an options trade.

        Returns an OrderPreview if successful, None if we can't build the
        OCC symbol or the broker call fails (caller falls back to direct fill).
        """
        if not (strike and expiry and option_type):
            return None

        try:
            occ = _build_occ_symbol(symbol, expiry, option_type, strike)
        except Exception as exc:
            log.warning("DiscordPaperBridge: could not build OCC symbol for %s: %s", symbol, exc)
            return None

        order_action = "BUY_TO_OPEN" if action in ("BTO", "BUY", "BUY_TO_OPEN") else "SELL_TO_OPEN"
        options_order = OptionsOrder(
            ticker        = symbol,
            action        = order_action,  # type: ignore[arg-type]
            contract      = occ,
            qty           = contracts,
            order_type    = "limit",
            limit_price   = limit_price,
            strategy_name = f"discord_{strategy}",
            account_id    = self._account.account_id,
        )

        try:
            preview = self._broker.preview_options_order(options_order)
            log.debug(
                "DiscordPaperBridge: Schwab preview %s %s  occ=%s  "
                "cost=$%.2f  bp_left=$%.2f  valid=%s",
                action, symbol, occ,
                preview.estimated_cost, preview.projected_available_fund,
                preview.is_valid,
            )
            return preview
        except Exception as exc:
            log.warning(
                "DiscordPaperBridge: Schwab preview failed for %s (%s) — falling back: %s",
                symbol, occ, exc,
            )
            return None


__all__ = ["DiscordPaperBridge"]
