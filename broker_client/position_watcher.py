"""Unified position watcher (Day 9, CLAUDE.md Section 10).

Watches ALL open positions regardless of strategy type.
Key behaviours:
  1. Startup load — from broker API + SQLite; broker is truth
  2. Auto-register — on confirmed fill from OrderManager
  3. Regime-aware closure — via on_regime_change()
  4. Strategy routing — each Position carries strategy_name
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from threading import Thread
from typing import Any

from broker_client.buckets.bucket_manager import BucketManager
from broker_client.buckets.exit_rules import (
    IMMEDIATE,
    ExitRecommendation,
    ExitRulesEngine,
)
from broker_client.buckets.models import BucketPosition
from broker_client.order_router import OrderRouter
from broker_client.position_manager import PositionManager
from broker_client.strategies import STRATEGY_REGISTRY
from broker_client.strategies.base_strategy import BaseStrategy
from broker_core.base_broker import BaseBroker, Order, OrderResult, Position
from config.settings import settings
from core.logger import get_logger
from signals.signal_schema import MarketSnapshot

logger = get_logger(__name__)


def _days_to_expiry(expiry: str) -> int:
    """Calendar days from today (UTC) to an ISO ``YYYY-MM-DD`` expiry (>= 0)."""
    try:
        exp = date.fromisoformat(expiry)
    except (ValueError, TypeError):
        return 0
    return max((exp - datetime.now(tz=UTC).date()).days, 0)


class PositionWatcher:
    """Continuously monitors open positions for exit conditions.

    Scan interval: POSITION_SCAN_INTERVAL_SECONDS (default 60s).
    """

    def __init__(
        self,
        broker: BaseBroker | None,
        router: OrderRouter,
        position_manager: PositionManager,
        alert_engine: Any = None,
        exit_rules: ExitRulesEngine | None = None,
        bucket_manager: BucketManager | None = None,
    ) -> None:
        self._broker = broker
        self._router = router
        self._pm = position_manager
        self._positions: dict[str, dict] = {}  # ticker → position dict
        self._running = False
        self._thread: Thread | None = None
        # Phase 2: bucket-aware exit alerts (lazily defaulted so existing
        # callers that don't pass these keep working).
        self._alerts = alert_engine
        self._exit_rules = exit_rules or ExitRulesEngine()
        self._buckets = bucket_manager or BucketManager()

    # ── Startup reconciliation ───────────────────────────────────────────────

    def startup_load(self) -> None:
        """Load open positions from broker API (truth) + SQLite (metadata).

        Broker API wins on what's open. SQLite has our strategy/stop metadata.
        """
        db_positions = {p["ticker"]: p for p in self._pm.get_open_positions()}

        if self._broker is not None:
            try:
                broker_positions = {p.ticker: p for p in self._broker.get_positions()}
            except Exception as exc:
                logger.warning("PositionWatcher: broker reconciliation failed: %s — using DB only", exc)
                broker_positions = {}
        else:
            broker_positions = {}

        # Broker is truth: merge broker positions with DB metadata
        merged: dict[str, dict] = {}
        for ticker, bp in broker_positions.items():
            db_meta = db_positions.get(ticker, {})
            merged[ticker] = {
                "ticker": ticker,
                "qty": bp.qty,
                "entry_price": bp.avg_cost,
                "current_price": bp.current_price,
                "strategy_name": db_meta.get("strategy", bp.strategy_name),
                "stop_loss": db_meta.get("stop_loss"),
                "take_profit": db_meta.get("take_profit"),
                "position_type": db_meta.get("position_type", bp.position_type),
            }

        # Include DB-only positions (not yet reflected in broker — e.g. paper mode)
        for ticker, dp in db_positions.items():
            if ticker not in merged:
                merged[ticker] = dp

        self._positions = merged
        logger.info(
            "PositionWatcher: loaded %d position(s) on startup (broker=%d, db=%d)",
            len(merged), len(broker_positions), len(db_positions),
        )

    # ── Auto-register ────────────────────────────────────────────────────────

    def register_if_filled(self, order: Order, result: OrderResult) -> None:
        """Called by OrderManager on fill — immediately tracks the new position."""
        if result.status not in ("filled", "partial"):
            return
        if order.action in ("BUY", "BUY_TO_OPEN"):
            self._positions[order.ticker] = {
                "ticker": order.ticker,
                "qty": order.qty,
                "entry_price": result.fill_price,
                "current_price": result.fill_price,
                "strategy_name": order.strategy_name,
                "stop_loss": None,
                "take_profit": None,
            }
            logger.debug("PositionWatcher: registered new position %s", order.ticker)
        elif order.action in ("SELL", "SELL_TO_CLOSE"):
            self._positions.pop(order.ticker, None)
            logger.debug("PositionWatcher: removed position %s after sell", order.ticker)

    def register(self, position: dict) -> None:
        """Directly register a position dict (used in tests)."""
        self._positions[position["ticker"]] = position

    # ── Market tick handler ───────────────────────────────────────────────────

    def on_market_tick(self, snapshot: MarketSnapshot) -> None:
        """Check all positions on each market tick for exit conditions."""
        pos_dict = self._positions.get(snapshot.ticker)
        if pos_dict is None:
            return

        # Build a Position object for strategy routing
        position = Position(
            ticker=pos_dict["ticker"],
            qty=pos_dict.get("qty", 0),
            avg_cost=pos_dict.get("entry_price", 0.0),
            current_price=snapshot.price,
            strategy_name=pos_dict.get("strategy_name", "unknown"),
            position_type=pos_dict.get("position_type", "equity_long"),
            stop_loss=pos_dict.get("stop_loss"),
            take_profit=pos_dict.get("take_profit"),
        )

        strategy_cls = STRATEGY_REGISTRY.get(position.strategy_name)
        if strategy_cls is not None:
            strategy = strategy_cls()
            if strategy.should_exit(position, snapshot):
                self._close(position, strategy, "strategy_exit")
                return

        # Generic stop/take-profit fallback
        if position.stop_loss and snapshot.price <= position.stop_loss:
            self._close_generic(position, "stop_loss")
        elif position.take_profit and snapshot.price >= position.take_profit:
            self._close_generic(position, "take_profit")

    def on_regime_change(self, new_regime: str) -> None:
        """Close positions whose strategies are triggered by this regime."""
        triggers = settings.REGIME_CLOSE_TRIGGERS.get(new_regime, [])
        if not triggers:
            return

        for ticker, pos_dict in list(self._positions.items()):
            strategy_name = pos_dict.get("strategy_name", "unknown")
            if strategy_name in triggers:
                logger.warning(
                    "PositionWatcher: regime=%s triggers close of %s (strategy=%s)",
                    new_regime, ticker, strategy_name,
                )
                position = Position(
                    ticker=ticker,
                    qty=pos_dict.get("qty", 0),
                    avg_cost=pos_dict.get("entry_price", 0.0),
                    current_price=pos_dict.get("current_price", 0.0),
                    strategy_name=strategy_name,
                )
                self._close_generic(position, f"regime_{new_regime}")

    def _close(self, position: Position, strategy: BaseStrategy, reason: str) -> None:
        from broker_core.base_broker import Order as _Order
        exit_order = strategy.build_exit_order(position)
        if isinstance(exit_order, _Order):
            result = self._router.execute(exit_order)
        else:
            result = self._router.execute_options(exit_order)
        self._positions.pop(position.ticker, None)
        logger.info(
            "PositionWatcher: closed %s via %s reason=%s fill=%.4f",
            position.ticker, strategy.strategy_name, reason, result.fill_price,
        )

    def _close_generic(self, position: Position, reason: str) -> None:
        from typing import Literal
        action: Literal["SELL", "BUY"] = "SELL" if position.qty > 0 else "BUY"
        exit_order = Order(
            ticker=position.ticker,
            action=action,
            qty=abs(position.qty),
            order_type="market",
            strategy_name=position.strategy_name,
        )
        result = self._router.execute(exit_order)
        self._positions.pop(position.ticker, None)
        logger.info(
            "PositionWatcher: generic close %s reason=%s fill=%.4f",
            position.ticker, reason, result.fill_price,
        )

    # ── 21 DTE alert system (Phase 2 Step 5) ─────────────────────────────────

    def _to_bucket_position(self, d: dict) -> BucketPosition:
        """Build a bucket-aware position from a watched position dict.

        Reads option metadata when present (expiry/strike/option_type) and
        classifies the position so the exit-rules engine can evaluate it.
        """
        strategy = d.get("strategy_name") or d.get("strategy") or ""
        expiry = d.get("expiry")
        dte = d.get("dte_remaining")
        if dte is None and expiry:
            dte = _days_to_expiry(expiry)
        dte = int(dte or 0)

        cls = self._buckets.classify_position(
            {"strategy": strategy, "dte_remaining": dte, "option_type": d.get("option_type")}
        )
        entry = float(d.get("entry_price") or 0.0)
        current = float(d.get("current_price") or 0.0)
        profit_pct = d.get("profit_pct")
        if profit_pct is None and entry > 0:
            # Short premium: profit as premium decays (entry − current)/entry.
            sign = -1.0 if str(d.get("position_type", "")).endswith("short") else 1.0
            profit_pct = sign * (current - entry) / entry * 100.0
        return BucketPosition(
            ticker=d["ticker"],
            strategy=strategy,
            bucket=int(d.get("bucket") or cls.bucket),
            sub_type=d.get("sub_type") or cls.sub_type,
            recoverable=bool(d.get("recoverable", cls.recoverable)),
            position_type=d.get("position_type", "equity_long"),
            option_type=d.get("option_type"),
            strike=d.get("strike"),
            expiry=expiry,
            qty=int(d.get("qty") or 1),
            entry_price=entry,
            current_price=current,
            underlying_price=d.get("underlying_price"),
            profit_pct=float(profit_pct or 0.0),
            dte_remaining=dte,
            original_dte=int(d.get("original_dte") or 0),
            days_held=int(d.get("days_held") or 0),
            days_to_earnings=d.get("days_to_earnings"),
            thesis_status=d.get("thesis_status", "active"),
        )

    def check_dte_alerts(self) -> list[ExitRecommendation]:
        """Fire 21-DTE / 7-DTE / immediate-exit alerts for option positions.

        Idempotent per position via ``alerted_21dte``/``alerted_7dte`` flags so
        each threshold alerts once. Returns the exit recommendations evaluated
        (useful for tests and the daily briefing). No-op without an AlertEngine.
        """
        fired: list[ExitRecommendation] = []
        for _ticker, d in list(self._positions.items()):
            # Only option positions have a DTE clock.
            if not (d.get("expiry") or d.get("dte_remaining") is not None):
                continue
            bp = self._to_bucket_position(d)
            dte = bp.dte_remaining
            rec = self._exit_rules.check_exit(bp)
            if rec is not None:
                fired.append(rec)

            if dte <= settings.DTE_EXIT_THRESHOLD and not d.get("alerted_21dte"):
                self._send(
                    f"⏰ 21 DTE ALERT: {bp.ticker}",
                    self._dte_body(bp, rec),
                    channel="signals",
                    color="yellow",
                )
                d["alerted_21dte"] = True

            if dte <= settings.DTE_URGENT_THRESHOLD and not d.get("alerted_7dte"):
                self._send(
                    f"🚨 URGENT {dte} DTE: {bp.ticker}",
                    f"Only {dte} days left — action required today",
                    channel="alerts",
                    color="red",
                )
                d["alerted_7dte"] = True

            if rec is not None and rec.urgency == IMMEDIATE:
                self._send(
                    f"💰 EXIT SIGNAL: {bp.ticker}",
                    f"{rec.reason}\nP&L: {bp.profit_pct:+.1f}%",
                    channel="opportunities",
                    color="green",
                )
        return fired

    @staticmethod
    def _dte_body(bp: BucketPosition, rec: ExitRecommendation | None) -> str:
        action = rec.action if rec else "HOLD"
        reason = rec.reason if rec else "n/a"
        contract = ""
        if bp.option_type and bp.strike:
            contract = f"{bp.strike:g}{bp.option_type[0].upper()} {bp.expiry or ''}".strip()
        return (
            f"Position: {bp.ticker} {contract}\n"
            f"Bucket: {bp.bucket} — {bp.sub_type}\n"
            f"Current P&L: {bp.profit_pct:+.1f}%\n"
            f"DTE remaining: {bp.dte_remaining}\n"
            f"Recommendation: {action}\n"
            f"Reason: {reason}"
        )

    def _send(self, title: str, body: str, channel: str, color: str) -> None:
        """Route through AlertEngine.send_alert when an engine is wired up."""
        if self._alerts is None:
            logger.debug("PositionWatcher: no AlertEngine — would alert: %s", title)
            return
        try:
            self._alerts.send_alert(title=title, body=body, channel=channel, color=color)
        except Exception as exc:
            logger.error("PositionWatcher: alert failed (%s): %s", title, exc)

    # ── Background scan loop ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start background scan thread."""
        self._running = True
        self._thread = Thread(target=self._scan_loop, daemon=True, name="position-watcher")
        self._thread.start()
        logger.info("PositionWatcher: started (interval=%ds)", settings.POSITION_SCAN_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("PositionWatcher: stopped")

    def _scan_loop(self) -> None:
        while self._running:
            try:
                self._refresh_prices()
            except Exception as exc:
                logger.error("PositionWatcher scan error: %s", exc)
            time.sleep(settings.POSITION_SCAN_INTERVAL_SECONDS)

    def _refresh_prices(self) -> None:
        """Fetch current prices and fire tick handlers for each position."""
        if not self._positions or self._broker is None:
            return
        from datetime import datetime

        for ticker in list(self._positions.keys()):
            try:
                quote = self._broker.get_quote(ticker)
                snapshot = MarketSnapshot(
                    timestamp=datetime.now(tz=UTC),
                    ticker=ticker,
                    price=quote.last or quote.ask or 0.0,
                    volume=quote.volume,
                    bid=quote.bid,
                    ask=quote.ask,
                )
                self.on_market_tick(snapshot)
            except Exception as exc:
                logger.warning("PositionWatcher: price refresh failed for %s: %s", ticker, exc)

    # ── Phase 4: Earnings overlap warning ────────────────────────────────────

    def check_earnings_overlaps(self) -> None:
        """Alert when an open option position has earnings within its expiry window.

        Call once daily at 8 AM ET. Reads earnings_opportunities table and
        cross-references open positions.
        """
        today = datetime.now(tz=UTC).date()

        for _ticker, d in list(self._positions.items()):
            expiry_str = d.get("expiry")
            if not expiry_str:
                continue
            try:
                exp = date.fromisoformat(str(expiry_str))
            except (ValueError, TypeError):
                continue

            ticker = d.get("ticker", "")
            if not ticker:
                continue

            earnings_date = self._get_next_earnings(ticker)
            if not earnings_date:
                continue

            # Check if earnings falls inside the remaining option window
            if not (today <= earnings_date <= exp):
                continue

            days_to_earn = (earnings_date - today).days
            severity = "CRITICAL" if days_to_earn <= 3 else "WARNING"
            bucket = d.get("bucket", 1)
            sub_type = d.get("sub_type", "msp")
            strike = d.get("strike", "")
            opt = str(d.get("option_type", "") or "").upper()[:1]
            urgent = "🚨 URGENT: Earnings in 3 days or less\n" if days_to_earn <= 3 else ""

            body = (
                f"Open position expires {exp}\n"
                f"{ticker} reports earnings {earnings_date} ({days_to_earn} days away)\n\n"
                f"Earnings fall INSIDE your expiry window.\n\n"
                f"Position: {ticker} {strike}{opt}\n"
                f"Bucket: {bucket} ({sub_type})\n\n"
                f"{urgent}"
                f"Suggested action:\n"
                f"  - Roll expiry beyond earnings date\n"
                f"  - OR close position before earnings\n"
                f"  - OR this is intentional earnings play (Bucket 2)"
            )
            self._send(
                title=f"⚠️ EARNINGS OVERLAP: {ticker}",
                body=body,
                channel="alerts" if severity == "CRITICAL" else "signals",
                color="red" if severity == "CRITICAL" else "yellow",
            )

    def _get_next_earnings(self, ticker: str) -> date | None:
        """Look up next earnings date from DB, then yfinance as fallback."""
        from data.db import get_connection as _gc
        try:
            with _gc(settings.DB_PATH) as conn:
                row = conn.execute(
                    "SELECT earnings_date FROM earnings_opportunities "
                    "WHERE ticker=? ORDER BY earnings_date LIMIT 1",
                    (ticker,),
                ).fetchone()
            if row and row["earnings_date"]:
                return date.fromisoformat(row["earnings_date"])
        except Exception as exc:
            logger.debug("PositionWatcher: earnings DB lookup failed for %s: %s", ticker, exc)
        # Fallback: yfinance calendar
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                return None
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if hasattr(ed, "__iter__") and not isinstance(ed, str):
                    ed = list(ed)[0] if ed else None
            else:
                try:
                    ed = cal.loc["Earnings Date"].iloc[0]
                except Exception:
                    return None
            if ed is None:
                return None
            if hasattr(ed, "date"):
                return ed.date()
            return date.fromisoformat(str(ed)[:10])
        except Exception as exc:
            logger.debug("PositionWatcher: yfinance earnings lookup failed for %s: %s", ticker, exc)
        return None

    # ── Phase 4: IV rank monitoring for open shorts ───────────────────────────

    def check_iv_rank_changes(self) -> None:
        """Alert when IV rank drops or spikes on open short positions.

        Call every 30 min during market hours. Reads iv_rank from the position
        dict (populated at entry) and fetches current IV rank from yfinance
        historical volatility as a proxy.
        """
        short_positions = [
            (t, d) for t, d in self._positions.items()
            if "short" in str(d.get("position_type", "") or "").lower()
        ]

        for _ticker, d in short_positions:
            ticker = d.get("ticker", "")
            if not ticker:
                continue

            current_iv_rank = self._get_current_iv_rank(ticker)
            if current_iv_rank is None:
                continue

            entry_iv_rank = float(d.get("iv_rank_at_entry") or d.get("iv_rank") or 50)
            iv_rank_change = current_iv_rank - entry_iv_rank
            unrealized_pct = d.get("profit_pct", 0.0) or 0.0
            strike = d.get("strike", "")
            opt = str(d.get("option_type", "") or "").upper()[:1]

            if current_iv_rank < settings.IV_RANK_EDGE_GONE_THRESHOLD and entry_iv_rank >= 50:
                self._send(
                    title=f"📉 IV RANK DROP: {ticker}",
                    body=(
                        f"Short position: {ticker} {strike}{opt}\n"
                        f"IV Rank at entry: {entry_iv_rank:.0f}\n"
                        f"IV Rank now: {current_iv_rank:.0f}\n\n"
                        f"Your premium-selling edge has diminished.\n"
                        f"Consider closing early to lock in remaining profit.\n"
                        f"Current P&L: {unrealized_pct:+.1f}%"
                    ),
                    channel="opportunities",
                    color="yellow",
                )

            elif current_iv_rank > settings.IV_RANK_SPIKE_THRESHOLD and iv_rank_change > 20:
                self._send(
                    title=f"⚠️ IV SPIKE: {ticker}",
                    body=(
                        f"Short position: {ticker} {strike}{opt}\n"
                        f"IV Rank at entry: {entry_iv_rank:.0f}\n"
                        f"IV Rank now: {current_iv_rank:.0f} (↑{iv_rank_change:.0f} points)\n\n"
                        f"IV spike is working against your short position.\n"
                        f"Review position and consider rolling or closing.\n"
                        f"Current P&L: {unrealized_pct:+.1f}%"
                    ),
                    channel="alerts",
                    color="red",
                )

    @staticmethod
    def _get_current_iv_rank(ticker: str) -> float | None:
        """Approximate IV rank from 252-day rolling HV as a proxy."""
        try:
            import yfinance as yf  # type: ignore[import-untyped]
            hist = yf.Ticker(ticker).history(period="1y")
            if hist is None or len(hist) < 30:
                return None
            log_ret = hist["Close"].pct_change().dropna()
            current_hv = float(log_ret.rolling(21).std().iloc[-1]) * (252 ** 0.5) * 100
            low_hv = float(log_ret.rolling(21).std().rolling(252).min().iloc[-1]) * (252 ** 0.5) * 100
            high_hv = float(log_ret.rolling(21).std().rolling(252).max().iloc[-1]) * (252 ** 0.5) * 100
            if high_hv == low_hv:
                return 50.0
            return (current_hv - low_hv) / (high_hv - low_hv) * 100.0
        except Exception as exc:
            logger.debug("PositionWatcher: IV rank calc failed for %s: %s", ticker, exc)
            return None

    @property
    def positions(self) -> dict[str, dict]:
        return dict(self._positions)


__all__ = ["PositionWatcher"]
