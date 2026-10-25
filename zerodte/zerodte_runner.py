"""zerodte/zerodte_runner.py

Standalone runner for the Ravish 0DTE QQQ put-selling strategy.

Each trading day:
  • ENTRY_TIME (09:45 ET): STO one QQQ 0DTE ~30-delta put, bracket TP/SL.
  • every MANAGE_INTERVAL_MIN: re-price the option; close on TP or SL.
  • FORCE_CLOSE_TIME (15:45 ET): close manually if still open (flat before 16:00).

Runs independently of the main engine and the Discord copy-trader — its own
paper account (paper_zerodte), its own Schwab account (optional), and its own
STANDALONE live/paper switch (ZERODTE_LIVE_TRADING).

PAPER mode books to the PaperAccount (a synthetic long whose cost basis = the
real Schwab margin, so buying power is realistic and capital releases on close).
LIVE mode places real Schwab orders.

Run:  python -m zerodte.zerodte_runner
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from broker_core.account_registry import get_broker_for, get_paper_account
from broker_core.base_broker import OptionsOrder, Order, OrderPreview, OrderResult
from broker_client.options_engine import OptionsEngine
from config.settings import settings
from zerodte import zerodte_strategy as strat

log = logging.getLogger(__name__)

_MULT = 100


class ZeroDTERunner:
    def __init__(self) -> None:
        self._account = get_paper_account(settings.ZERODTE_PAPER_ACCOUNT)
        self._broker  = get_broker_for(settings.ZERODTE_SCHWAB_ACCOUNT or None)
        self._engine  = OptionsEngine(self._broker)
        self._ticker  = settings.ZERODTE_TICKER
        self._live    = bool(getattr(settings, "ZERODTE_LIVE_TRADING", False))
        self._open: Optional[strat.ZeroDTEPlan] = None   # current day's open plan
        self._oco_id: Optional[str] = None               # resting broker OCO (live)
        self._broker_managed = False                     # TP/SL rest at the broker?
        mode = "LIVE" if self._live else "PAPER"
        log.warning(
            "ZeroDTERunner: %s  ticker=%s  account=%s  delta=%.2f  TP=%.0f%%  SL=%.0f%%",
            mode, self._ticker, settings.ZERODTE_PAPER_ACCOUNT,
            settings.ZERODTE_TARGET_DELTA, settings.ZERODTE_TAKE_PROFIT_PCT,
            settings.ZERODTE_STOP_LOSS_PCT,
        )

    # ── Entry ───────────────────────────────────────────────────────────────

    def open_position(self, today: Optional[date] = None) -> bool:
        """STO the 0DTE 30-delta put and arm the bracket. Returns True if opened."""
        today = today or datetime.now(tz=timezone.utc).date()
        if self._open is not None:
            log.info("ZeroDTE: position already open — skipping entry")
            return False
        if (settings.ZERODTE_SKIP_EVENT_DAYS
                and strat.is_event_day(today, settings.ZERODTE_EVENT_DATES)):
            log.info("ZeroDTE: %s is a configured event day — skipping", today)
            return False

        expiry = today.isoformat()   # 0DTE = today's expiration
        try:
            chain = self._engine.fetch_chain(self._ticker, expiry=expiry)
        except Exception as exc:  # noqa: BLE001
            log.error("ZeroDTE: chain fetch failed for %s %s: %s", self._ticker, expiry, exc)
            return False

        contract = strat.select_put(chain, settings.ZERODTE_TARGET_DELTA)
        if contract is None:
            log.warning("ZeroDTE: no put with delta data in %s chain", self._ticker)
            return False

        qty = settings.ZERODTE_CONTRACTS
        if settings.ZERODTE_AUTOSIZE:
            equity = self._account.get_state().cash
            qty = strat.size_contracts(equity, settings.ZERODTE_CAPITAL_PER_CONTRACT,
                                       settings.ZERODTE_MAX_CONTRACTS)
        if qty < 1:
            log.warning("ZeroDTE: sized to 0 contracts — skipping")
            return False

        plan = strat.build_plan(contract, settings.ZERODTE_TAKE_PROFIT_PCT,
                                settings.ZERODTE_STOP_LOSS_PCT, qty)
        if plan is None:
            log.warning("ZeroDTE: no credit on selected put — skipping")
            return False

        if not self._execute_open(plan):
            return False
        self._open = plan
        log.warning(
            "ZeroDTE OPEN %s %s put strike=%.1f  credit=%.2f  qty=%d  "
            "TP=%.2f  SL=%.2f  mode=%s",
            self._ticker, expiry, plan.contract.strike, plan.credit, qty,
            plan.tp_price, plan.sl_price, "LIVE" if self._live else "paper",
        )
        return True

    # ── Manage ──────────────────────────────────────────────────────────────

    def manage(self) -> None:
        """Manage the open position.

        LIVE with a resting broker OCO: the exchange owns the TP/SL exit, so we
        do nothing here (the whole point of the OCO — survives this process
        being down). PAPER (or a LIVE fallback where the OCO couldn't be placed)
        polls the option price and closes on TP/SL.
        """
        if self._open is None:
            return
        if self._live and self._broker_managed:
            return   # broker's OCO handles TP/SL
        price = self._current_price(self._open.contract.symbol, self._open.contract.expiry)
        if price is None:
            log.warning("ZeroDTE: could not re-price %s — will retry", self._open.contract.symbol)
            return
        reason = strat.exit_reason(price, self._open)
        if reason:
            self.close_position(reason, price)

    def force_close(self) -> None:
        """Flatten if still open — must be flat before 16:00 ET. There is no
        broker-resting timed order (the Schwab API has no time trigger), so this
        scheduled step cancels any resting OCO and buys the put back."""
        if self._open is None:
            return
        price = self._current_price(self._open.contract.symbol, self._open.contract.expiry)
        if price is None:
            price = self._open.credit   # best effort
        if self._live and self._oco_id:
            try:
                self._broker.cancel_order(self._oco_id)
                log.warning("ZeroDTE: cancelled resting OCO %s before force-close", self._oco_id)
            except Exception as exc:  # noqa: BLE001
                log.error("ZeroDTE: cancel OCO failed (%s) — the OCO may already have filled", exc)
        self.close_position("force_close_eod", price)

    def close_position(self, reason: str, exit_price: float) -> None:
        plan = self._open
        if plan is None:
            return
        pnl = strat.realized_pnl(plan, exit_price)
        self._execute_close(plan, exit_price, reason)
        log.warning(
            "ZeroDTE CLOSE %s  strike=%.1f  exit=%.2f  P&L=$%.2f  reason=%s  mode=%s",
            self._ticker, plan.contract.strike, exit_price, pnl, reason,
            "LIVE" if self._live else "paper",
        )
        self._open = None
        self._oco_id = None
        self._broker_managed = False

    # ── Execution ─────────────────────────────────────────────────────────────

    def _execute_open(self, plan: strat.ZeroDTEPlan) -> bool:
        """STO the put, then arm the bracket.

        LIVE: place the real STO, then rest a TP/SL OCO at the broker so the exit
        is handled all day without this process (the 3:45 close stays a scheduled
        action — the API has no resting timed order). Schwab is the book of
        record in live, so no paper-ledger mirror is written.
        PAPER: book a synthetic long (cost basis = real Schwab margin) and poll.
        """
        occ = plan.contract.symbol
        sto = OptionsOrder(
            ticker=self._ticker, action="SELL_TO_OPEN", contract=occ,
            qty=plan.contracts, order_type="limit", limit_price=plan.credit,
            strategy_name="zerodte_qqq", account_id=self._account.account_id,
        )
        margin = plan.credit * plan.contracts * _MULT   # fallback if no preview
        try:
            preview = self._broker.preview_options_order(sto)
            if preview is not None and not preview.is_valid:
                log.warning("ZeroDTE: broker rejected preview — %s", preview.rejection_reason)
                return False
            margin = self._margin_from(preview, margin)
        except Exception as exc:  # noqa: BLE001
            log.warning("ZeroDTE: preview failed (%s) — using notional margin %.2f", exc, margin)

        if self._live:
            try:
                res = self._broker.place_options_order(sto)
                log.warning("ZeroDTE: LIVE STO id=%s status=%s",
                            getattr(res, "order_id", "?"), getattr(res, "status", "?"))
            except Exception as exc:  # noqa: BLE001
                log.error("ZeroDTE: LIVE STO FAILED: %s", exc)
                return False
            # Rest the TP/SL OCO at the broker (survives this process going down).
            self._broker_managed = False
            self._oco_id = None
            place_oco = getattr(self._broker, "place_options_oco_close", None)
            if callable(place_oco):
                try:
                    oco = place_oco(occ, plan.contracts, plan.tp_price, plan.sl_price)
                    self._oco_id = getattr(oco, "order_id", None)
                    self._broker_managed = True
                    log.warning("ZeroDTE: resting OCO armed id=%s (TP=%.2f SL=%.2f)",
                                self._oco_id, plan.tp_price, plan.sl_price)
                except Exception as exc:  # noqa: BLE001
                    log.error("ZeroDTE: OCO placement failed (%s) — falling back to "
                              "poll-managed exit (process must stay up)", exc)
            else:
                log.warning("ZeroDTE: broker has no OCO support — poll-managed exit")
            return True   # live: Schwab is the book of record, no paper mirror

        # PAPER: synthetic-long ledger (cost basis = margin), poll-managed.
        state = self._account.get_state()
        if state.cash < margin:
            log.warning("ZeroDTE: insufficient paper cash ($%.2f < $%.2f) — skipping",
                        state.cash, margin)
            return False
        book = Order(ticker=self._ticker, action="BUY", qty=plan.contracts,
                     order_type="limit", limit_price=plan.credit,
                     strategy_name="zerodte_qqq", account_id=self._account.account_id)
        self._account.apply_preview(
            book, OrderPreview(estimated_cost=margin, is_valid=True, fees=0.0))
        return True

    def _execute_close(self, plan: strat.ZeroDTEPlan, exit_price: float, reason: str) -> None:
        occ = plan.contract.symbol
        if self._live:
            # Options market orders are blocked by the broker — use a marketable
            # LIMIT at the current price to flatten.
            try:
                res = self._broker.place_options_order(OptionsOrder(
                    ticker=self._ticker, action="BUY_TO_CLOSE", contract=occ,
                    qty=plan.contracts, order_type="limit",
                    limit_price=round(max(exit_price, 0.01), 2),
                    strategy_name=f"zerodte_{reason}", account_id=self._account.account_id))
                log.warning("ZeroDTE: LIVE BTC id=%s status=%s",
                            getattr(res, "order_id", "?"), getattr(res, "status", "?"))
            except Exception as exc:  # noqa: BLE001
                log.error("ZeroDTE: LIVE BTC FAILED: %s", exc)
            return   # live: no paper-ledger mirror

        # PAPER close: release cost basis + premium P&L (symmetric with the open).
        pnl = strat.realized_pnl(plan, exit_price)
        pos = next((p for p in self._account.get_state().open_positions
                    if p.ticker == self._ticker), None)
        if pos is not None and pos.qty > 0:
            cost_basis = pos.qty * pos.entry_price
            self._account.apply_fill(
                Order(ticker=self._ticker, action="SELL", qty=pos.qty,
                      order_type="market", strategy_name=f"zerodte_{reason}",
                      account_id=self._account.account_id),
                fill_price=(cost_basis + pnl) / pos.qty, commission=0.0)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _margin_from(self, preview: Optional[OrderPreview], fallback: float) -> float:
        """Real buying-power reduction (available_funds − projected). 1× (no
        buffer — this strategy's risk is the stop, not held cash)."""
        if preview is None:
            return fallback
        try:
            af = float(getattr(self._broker.get_account(), "available_funds", 0.0) or 0.0)
            proj = float(preview.projected_available_fund or 0.0)
            red = af - proj
            if af > 0 and 0 < red < af:
                return red
        except Exception:  # noqa: BLE001
            pass
        return abs(float(preview.estimated_cost or 0.0)) or fallback

    def _current_price(self, occ: str, expiry: str) -> Optional[float]:
        """Current mid of our contract by re-fetching the 0DTE chain."""
        try:
            chain = self._engine.fetch_chain(self._ticker, expiry=expiry)
        except Exception as exc:  # noqa: BLE001
            log.warning("ZeroDTE: re-fetch chain failed: %s", exc)
            return None
        for p in chain.puts:
            if p.symbol == occ:
                if p.bid and p.ask:
                    return (p.bid + p.ask) / 2
                return p.last or None
        return None


# ── Scheduler entry point ─────────────────────────────────────────────────────

def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    if not settings.ZERODTE_ENABLED:
        log.warning("ZERODTE_ENABLED=False — not starting. Set it True to run.")
        return

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from watcher.calendar_guard import CalendarGuard

    runner = ZeroDTERunner()
    guard  = CalendarGuard()
    eh, em = (int(x) for x in settings.ZERODTE_ENTRY_TIME.split(":"))
    fh, fm = (int(x) for x in settings.ZERODTE_FORCE_CLOSE_TIME.split(":"))
    sched  = BlockingScheduler(timezone="America/New_York")

    def _entry():
        if guard.is_trading_day():
            runner.open_position()

    def _manage():
        if guard.is_trading_day():
            runner.manage()

    def _close():
        if guard.is_trading_day():
            runner.force_close()

    sched.add_job(_entry, CronTrigger(hour=eh, minute=em, day_of_week="mon-fri"))
    sched.add_job(_manage, CronTrigger(hour=f"{eh}-{fh}",
                                       minute=f"*/{settings.ZERODTE_MANAGE_INTERVAL_MIN}",
                                       day_of_week="mon-fri"))
    sched.add_job(_close, CronTrigger(hour=fh, minute=fm, day_of_week="mon-fri"))
    log.warning("ZeroDTE scheduler started (ET): entry %02d:%02d  close %02d:%02d  manage every %dm",
                eh, em, fh, fm, settings.ZERODTE_MANAGE_INTERVAL_MIN)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.warning("ZeroDTE scheduler stopped")


if __name__ == "__main__":
    main()
