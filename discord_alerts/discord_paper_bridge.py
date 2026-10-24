"""discord_alerts/discord_paper_bridge.py

Bridges Discord signal trades to the PaperAccount using Schwab previewOrder.

When a tradeable Discord signal arrives, this bridge:
  1. Builds an OptionsOrder from the ParsedSignal fields
  2. Calls SchwabBroker.preview_options_order() — real Schwab API call, no money moves
  3. Gets back real margin/buying-power impact from Schwab
  4. Applies that cost to the paper_discord PaperAccount

MSP (Margin Secured Put / STO put) buffer rule:
  Schwab preview returns estimated_cost = margin required ($X).
  For MSP we book 2X from the paper account — the actual margin ($X) as the
  position cost, plus another $X held as a conservative buffer.  This reflects
  the fact that a short put has theoretically unlimited downside and we want to
  keep dry powder.  The 2X multiplier is configurable via MSP_MARGIN_MULTIPLIER.

All other options (BTO calls, BTO puts, BTO spreads):
  Book exactly what Schwab says (estimated_cost), no multiplier.

Usage — called from discord_user_listener.py after a signal is saved:

    bridge = DiscordPaperBridge()

    # On entry signal:
    bridge.open_trade(signal)

    # On exit (from _close_trade in exit_rules):
    bridge.close_trade(symbol, entry_price, exit_price, contracts, action, reason)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

import yaml

from broker_core.account_registry import get_broker_for, get_paper_account
from broker_core.base_broker import OptionsOrder, Order, OrderPreview, OrderResult
from config.settings import settings
from discord_alerts.capital_manager import pick_rotations

log = logging.getLogger(__name__)

_EXIT_RULES_PATH = os.path.join(os.path.dirname(__file__), "exit_rules.yaml")


def _dte_between(start_str, expiry_str):
    """Days from start date to expiry; None if either is missing/unparseable."""
    if not expiry_str:
        return None
    try:
        exp = date.fromisoformat(expiry_str[:10])
    except (ValueError, TypeError):
        return None
    if not start_str:
        return (exp - date.today()).days
    try:
        return (exp - date.fromisoformat(start_str[:10])).days
    except (ValueError, TypeError):
        return (exp - date.today()).days


def _load_exit_rules() -> dict:
    """Load exit_rules.yaml rules (for rotation PT lookups). Empty on failure."""
    try:
        with open(_EXIT_RULES_PATH) as f:
            return (yaml.safe_load(f) or {}).get("rules", {})
    except Exception as exc:  # noqa: BLE001
        log.warning("DiscordPaperBridge: could not load exit_rules.yaml: %s", exc)
        return {}


def _match_exit_rule(rules: dict, action: str, symbol: str, strategy: str,
                     dte_at_entry: Optional[int]) -> dict:
    """Mirror ExitRuleEngine._match_rule — pick the rule for a trade."""
    action = (action or "").upper(); symbol = (symbol or "").upper()
    strategy = (strategy or "").lower()
    for name, rule in rules.items():
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
                if strategy not in [s.lower() for s in st]:
                    continue
            elif strategy != st.lower():
                continue
        if "min_dte_at_entry" in m:
            if dte_at_entry is None or dte_at_entry < m["min_dte_at_entry"]:
                continue
        return rule
    return rules.get("default", {"profit_target_pct": 50})

# One options contract = 100 shares × price
_OPTIONS_MULTIPLIER = 100

# For MSP (STO put): multiply Schwab's margin requirement by this factor
# before debiting the paper account.  2.0 = block 2× margin as a buffer.
# Override via MSP_MARGIN_MULTIPLIER in .env.
_DEFAULT_MSP_MULTIPLIER = 2.0

# Strategy types that are treated as MSP (margin secured put).
_MSP_ACTIONS    = {"STO", "SELL", "SELL_TO_OPEN"}
_MSP_STRATEGIES = {"short_put", "short_put_leaps", "cash_secured_put", "msp"}


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
        acct_id              = account_id or settings.DISCORD_PAPER_ACCOUNT
        self._account        = get_paper_account(acct_id)
        self._broker         = get_broker_for(settings.DISCORD_SCHWAB_ACCOUNT or None)
        self._msp_multiplier = float(getattr(settings, "MSP_MARGIN_MULTIPLIER", _DEFAULT_MSP_MULTIPLIER))
        self._db_path        = settings.DB_PATH

        # Capital rotation threshold (profit %); 0 / unset = OFF.
        thr = float(getattr(settings, "DISCORD_ROTATION_THRESHOLD", 0.0) or 0.0)
        self._rotation_threshold: Optional[float] = thr if thr > 0 else None

        # STANDALONE Discord live/paper switch — independent of the main system's
        # ENV / DRY_RUN / LIVE_TRADING_ENABLED gate, so the Discord copy-trader can
        # run live while the rest of the engine stays in paper (or vice versa).
        # True → real Schwab orders for Discord trades. Default False → paper.
        # It is a single explicit bool (defaults off); nothing else gates it.
        self._live = bool(getattr(settings, "DISCORD_LIVE_TRADING", False))
        # Exit rules (for rotation: new-trade PT and candidates' remaining profit).
        self._rules = _load_exit_rules()

        if self._live:
            log.warning(
                "DiscordPaperBridge: *** DISCORD LIVE TRADING ENABLED *** "
                "(DISCORD_LIVE_TRADING=True, standalone) account=%s broker=%s "
                "— real Schwab orders will be placed for Discord trades. "
                "(UNTESTED path: paper-verify first.)",
                acct_id, type(self._broker).__name__,
            )
        else:
            log.info(
                "DiscordPaperBridge: PAPER mode  account=%s  broker=%s  "
                "msp_multiplier=%.1fx  rotation=%s",
                acct_id, type(self._broker).__name__, self._msp_multiplier,
                f"≥{self._rotation_threshold:.0f}%" if self._rotation_threshold else "off",
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
        preview, options_order = self._schwab_preview(
            symbol=symbol, action=action, contracts=contracts,
            limit_price=entry_price, strike=strike,
            expiry=expiry, option_type=option_type,
            strategy=strategy,
        )

        if preview is not None:
            if not preview.is_valid:
                log.warning(
                    "DiscordPaperBridge: Schwab rejected preview for %s — %s",
                    symbol, preview.rejection_reason,
                )
                return False

            is_msp = (
                action in _MSP_ACTIONS
                and (option_type or "").upper().startswith("P")
                and strategy.lower() in _MSP_STRATEGIES
            )

            if is_msp:
                # MSP: capital tied up by a short put is the REAL buying-power /
                # available-funds reduction Schwab projects — NOT the premium
                # (orderValue, which the preview maps to estimated_cost).
                #
                # Mirror llm_roi_analyzer._broker_margin_per_contract:
                #   margin = available_funds − projected_available_fund
                # using AVAILABLE FUNDS (real cash basis), not buying power
                # (buying power is leveraged ~4× and overstates margin).
                #
                # Then book margin × multiplier (default 2×) as the conservative
                # buffer so the paper account keeps dry powder.
                schwab_margin = self._msp_margin(preview)
                effective_cost = schwab_margin * self._msp_multiplier
                state = self._account.get_state()
                if state.cash < effective_cost:
                    # Try to free buying power by rotating out of an aged winner.
                    if not self._attempt_rotation(symbol, action, strategy, expiry,
                                                  entry_price, contracts, effective_cost):
                        log.warning(
                            "DiscordPaperBridge: insufficient cash for MSP %s "
                            "(need $%.2f [%.0fx $%.2f margin], have $%.2f) — skipping",
                            symbol, effective_cost, self._msp_multiplier,
                            schwab_margin, state.cash,
                        )
                        return False
                # Patch the preview so apply_preview() deducts the full 2X amount.
                # fill_price = estimated_cost / qty, so we set estimated_cost = 2X margin.
                adjusted_preview = preview.model_copy(update={"estimated_cost": effective_cost})
                # Book as a synthetic LONG in the (long-only) PaperAccount: the
                # position is a bookkeeping proxy for the capital/margin held, so
                # the open debits effective_cost and the close releases it. (An
                # STO/"SELL" here would be a no-op — apply_fill would try to close
                # a long that doesn't exist.)
                order = Order(
                    ticker        = symbol,
                    action        = "BUY",
                    qty           = contracts,
                    order_type    = "limit",
                    limit_price   = entry_price,
                    strategy_name = f"discord_{strategy}",
                    account_id    = self._account.account_id,
                )
                result = self._execute_open(options_order, order, adjusted_preview,
                                            f"MSP {symbol}")
                log.info(
                    "DiscordPaperBridge: OPEN MSP %s  contracts=%d  "
                    "schwab_margin=$%.2f  booked=%.0fx=$%.2f  mode=%s  status=%s",
                    symbol, contracts,
                    schwab_margin, self._msp_multiplier, effective_cost,
                    "LIVE" if self._live else "paper", result.status,
                )
            else:
                # BTO (calls, puts, spreads): book exactly what Schwab says.
                order = Order(
                    ticker        = symbol,
                    action        = "BUY",
                    qty           = contracts,
                    order_type    = "limit",
                    limit_price   = entry_price,
                    strategy_name = f"discord_{strategy}",
                    account_id    = self._account.account_id,
                )
                result = self._execute_open(options_order, order, preview,
                                            f"BTO {symbol}")
                log.info(
                    "DiscordPaperBridge: OPEN BTO %s  contracts=%d  "
                    "schwab_cost=$%.2f  buying_power_left=$%.2f  mode=%s  status=%s",
                    symbol, contracts,
                    preview.estimated_cost, preview.projected_available_fund,
                    "LIVE" if self._live else "paper", result.status,
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

        # Synthetic long (bookkeeping proxy for capital), same as the preview path.
        order = Order(
            ticker        = symbol,
            action        = "BUY",
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
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        option_type: Optional[str] = None,
    ) -> bool:
        """Record a trade close in PaperAccount (and place a real closing order
        when LIVE and the contract fields are available).

        The action is the ENTRY action (BTO/STO); the bridge derives the
        closing action automatically.
        """
        if not exit_price or not entry_price:
            log.warning("DiscordPaperBridge: missing price for close %s — skipping", symbol)
            return False

        contracts    = contracts or 1
        # Everything is booked as a synthetic long, so every close is a SELL of
        # that long (the premium P&L below carries the real economic result).
        close_action = "SELL"

        # LIVE: place a real closing order when we can build the contract.
        if self._live and strike and expiry and option_type:
            try:
                occ = _build_occ_symbol(symbol, expiry, option_type, strike)
                occ_close = "SELL_TO_CLOSE" if action in ("BTO", "BUY", "BUY_TO_OPEN") \
                    else "BUY_TO_CLOSE"
                live = self._broker.place_options_order(OptionsOrder(
                    ticker=symbol, action=occ_close, contract=occ, qty=contracts,
                    order_type="market", strategy_name=f"discord_{reason}",
                    account_id=self._account.account_id))
                log.warning("DiscordPaperBridge: LIVE CLOSE %s id=%s status=%s",
                            symbol, getattr(live, "order_id", "?"),
                            getattr(live, "status", "?"))
            except Exception as exc:  # noqa: BLE001
                log.error("DiscordPaperBridge: LIVE CLOSE FAILED %s: %s", symbol, exc)

        # Premium P&L (the real economic result on the option).
        pnl = (exit_price - entry_price) * contracts * _OPTIONS_MULTIPLIER
        if action in ("STO", "SELL", "SELL_TO_OPEN"):
            pnl = -pnl  # STO profits when price goes down

        # Release the position's ACTUAL cost basis (the capital/margin held at
        # open) plus the premium P&L — symmetric with the open. We locate the
        # open paper position for this ticker to get its true cost basis
        # (qty × entry_price), because MSP opens book 2×margin as the basis
        # while BTO opens book the premium notional. Closing with the position's
        # own qty makes apply_fill credit cost_basis + pnl back to cash exactly.
        pos = next((p for p in self._account.get_state().open_positions
                    if p.ticker == symbol), None)
        if pos is not None and pos.qty > 0:
            cost_basis = pos.qty * pos.entry_price
            close_fill = (cost_basis + pnl) / pos.qty
            order = Order(
                ticker=symbol, action=close_action, qty=pos.qty,  # type: ignore[arg-type]
                order_type="market", strategy_name=f"discord_{reason}",
                account_id=self._account.account_id,
            )
            self._account.apply_fill(order, fill_price=close_fill, commission=0.0)
        else:
            # No matching open position (e.g. already closed) — best-effort.
            log.warning("DiscordPaperBridge: no open paper position for %s at close", symbol)

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

    def _msp_margin(self, preview: OrderPreview) -> float:
        """Real per-order margin (buying-power reduction) for a short put.

        margin = available_funds − projected_available_fund

        Mirrors llm_roi_analyzer._broker_margin_per_contract: we use AVAILABLE
        FUNDS (real cash basis), not buying power (leveraged ~4×, would
        overstate margin several-fold).  Falls back to estimated_cost (the
        premium notional) if the broker/account data isn't usable.
        """
        try:
            account    = self._broker.get_account()
            current_af = float(getattr(account, "available_funds", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001 — degrade to premium notional
            log.warning("DiscordPaperBridge: get_account failed for MSP margin: %s", exc)
            current_af = 0.0

        projected_af = float(preview.projected_available_fund or 0.0)
        reduction    = current_af - projected_af

        # Sanity: a single order can't tie up more than all available funds,
        # and the reduction must be positive to be meaningful.
        if current_af > 0 and 0 < reduction < current_af:
            return reduction

        # Fallback — premium notional (last resort; better than nothing).
        log.warning(
            "DiscordPaperBridge: implausible MSP margin (af=%.2f proj=%.2f) "
            "— falling back to estimated_cost $%.2f",
            current_af, projected_af, preview.estimated_cost,
        )
        return preview.estimated_cost

    def _execute_open(self, options_order, book_order, book_preview, label: str):
        """Open a position: LIVE places a real Schwab order then mirrors to the
        paper ledger; PAPER books to the ledger only. Returns the paper result."""
        if self._live and options_order is not None:
            try:
                live = self._broker.place_options_order(options_order)
                log.warning("DiscordPaperBridge: LIVE OPEN [%s] id=%s status=%s",
                            label, getattr(live, "order_id", "?"),
                            getattr(live, "status", "?"))
            except Exception as exc:  # noqa: BLE001
                log.error("DiscordPaperBridge: LIVE OPEN FAILED [%s]: %s", label, exc)
                return OrderResult(order_id="LIVE_FAILED", status="rejected", fill_price=0.0)
        # Always mirror to the paper ledger (tracking account / dashboard parity).
        return self._account.apply_preview(book_order, book_preview)

    def _attempt_rotation(self, symbol, action, strategy, expiry,
                          entry_price, contracts, need_cost) -> bool:
        """Free buying power for a new signal by closing an aged winner, using
        capital_manager.pick_rotations. Returns True if enough was freed."""
        if self._rotation_threshold is None:
            return False
        state = self._account.get_state()
        if state.cash >= need_cost:
            return True

        # Expected profit of the NEW trade if it reaches its own PT.
        nrule = _match_exit_rule(self._rules, action, symbol, strategy,
                                 _dte_between(None, expiry))
        npt = float(nrule.get("profit_target_pct", 50))
        new_expected = entry_price * (npt / 100.0) * _OPTIONS_MULTIPLIER * (contracts or 1)

        # Candidates: open DB trades with a current mark, joined to the paper
        # position's cost basis (capital that would be freed).
        pos_by_ticker = {p.ticker: p for p in state.open_positions}
        cands, by_id = [], {}
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM discord_signals WHERE paper_status='open' "
                    "AND paper_pnl_pct IS NOT NULL"
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("DiscordPaperBridge: rotation DB read failed: %s", exc)
            return False

        for r in rows:
            sym = (r["symbol"] or "").upper()
            pos = pos_by_ticker.get(sym)
            if pos is None:
                continue
            mark = r["paper_pnl_pct"]
            if mark is None:
                continue
            capital = pos.qty * pos.entry_price            # cost basis freed on close
            c_entry = r["paper_entry"] or 0.0
            c_ctr   = r["contracts"] or 1
            crule = _match_exit_rule(self._rules, r["action"], sym,
                                     r["strategy_type"] or "",
                                     _dte_between(r["received_at"], r["expiry_short"]))
            cpt = float(crule.get("profit_target_pct", 50))
            remaining = c_entry * max(cpt - mark, 0) / 100.0 * _OPTIONS_MULTIPLIER * c_ctr
            cands.append({"id": r["id"], "mark_pct": mark, "capital": capital,
                          "remaining_pnl": remaining})
            by_id[r["id"]] = (r, mark)

        chosen = pick_rotations(need_cost, state.cash, new_expected,
                                cands, self._rotation_threshold)
        if not chosen:
            return False

        for cid in chosen:
            r, mark = by_id[cid]
            self._rotate_close(r, mark)
        return self._account.get_state().cash >= need_cost

    def _rotate_close(self, row, mark_pct) -> None:
        """Close one open trade early for capital rotation: LIVE closing order
        (if live), paper ledger close, and DB mark-closed."""
        sym      = (row["symbol"] or "?").upper()
        entry    = row["paper_entry"] or 0.0
        act      = (row["action"] or "BTO").upper()
        ctr      = row["contracts"] or 1
        exit_px  = entry * (1 - mark_pct / 100) if act in ("STO", "SELL", "SELL_TO_OPEN") \
            else entry * (1 + mark_pct / 100)
        log.warning("DiscordPaperBridge: ROTATION close %s @ %.1f%% (free capital)",
                    sym, mark_pct)
        # LIVE: place a real closing order when we can build the contract.
        if self._live and row["strike"] and row["expiry_short"] and row["option_type"]:
            try:
                occ = _build_occ_symbol(sym, row["expiry_short"], row["option_type"],
                                        row["strike"])
                close_act = "SELL_TO_CLOSE" if act in ("BTO", "BUY", "BUY_TO_OPEN") \
                    else "BUY_TO_CLOSE"
                self._broker.place_options_order(OptionsOrder(
                    ticker=sym, action=close_act, contract=occ, qty=ctr,
                    order_type="market", strategy_name="discord_rotation",
                    account_id=self._account.account_id))
            except Exception as exc:  # noqa: BLE001
                log.error("DiscordPaperBridge: LIVE rotation close FAILED %s: %s", sym, exc)
        # Paper ledger close.
        self.close_trade(sym, entry, exit_px, ctr, act, reason="rotation")
        # Mark closed in the DB so the exit engine stops managing it.
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "UPDATE discord_signals SET paper_status='closed', paper_exit=?, "
                    "paper_pnl_pct=?, closed_at=?, skip_reason='rotation' WHERE id=?",
                    (round(exit_px, 4), round(mark_pct, 2),
                     datetime.now(timezone.utc).isoformat(), row["id"]),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("DiscordPaperBridge: rotation DB close failed for #%s: %s",
                        row["id"], exc)

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
    ) -> tuple[Optional[OrderPreview], Optional[OptionsOrder]]:
        """Call Schwab previewOrder for an options trade.

        Returns (preview, options_order). Both are None if we can't build the
        OCC symbol or the broker call fails (caller falls back to direct fill).
        The options_order is returned so a LIVE open can place that same order.
        """
        if not (strike and expiry and option_type):
            return None, None

        try:
            occ = _build_occ_symbol(symbol, expiry, option_type, strike)
        except Exception as exc:
            log.warning("DiscordPaperBridge: could not build OCC symbol for %s: %s", symbol, exc)
            return None, None

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
            return preview, options_order
        except Exception as exc:
            log.warning(
                "DiscordPaperBridge: Schwab preview failed for %s (%s) — falling back: %s",
                symbol, occ, exc,
            )
            return None, options_order


__all__ = ["DiscordPaperBridge"]
