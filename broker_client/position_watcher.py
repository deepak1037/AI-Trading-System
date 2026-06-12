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

    @property
    def positions(self) -> dict[str, dict]:
        return dict(self._positions)


__all__ = ["PositionWatcher"]
