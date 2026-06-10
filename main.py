"""Entry point (CLAUDE.md Section 4): starts watcher + position watcher."""

from __future__ import annotations

import sys
import time

from config.settings import settings
from core.logger import get_logger
from data.db import init_db

logger = get_logger(__name__)


def _macro_alert_msg(signal: object) -> str:
    """Format a macro-surprise alert for Discord #alerts."""
    md = getattr(signal, "metadata", {})
    return (
        f"MACRO SURPRISE — {md.get('release', '?')} {md.get('period', '')}: "
        f"actual={md.get('actual_yoy', '?')}% consensus={md.get('consensus_yoy', '?')}% "
        f"(z={md.get('z_score', '?')}) → {getattr(signal, 'direction', '?')} "
        f"confidence={getattr(signal, 'confidence', '?')}"
    )


def _geo_alert_msg(signal: object) -> str:
    """Format a geopolitical-shock alert for Discord #alerts."""
    md = getattr(signal, "metadata", {})
    sample = md.get("sample_headlines", []) or []
    return (
        f"⚠️ GEOPOLITICAL SHOCK — risk-off. keywords={md.get('keywords', [])} "
        f"sentiment_avg={md.get('matched_avg', '?')} → {getattr(signal, 'direction', '?')} "
        f"confidence={getattr(signal, 'confidence', '?')}. "
        f"Headlines: {' | '.join(sample[:3])}"
    )


def main() -> None:
    logger.info(
        "AI-Trading-System starting | ENV=%s | BROKER=%s | DRY_RUN=%s",
        settings.ENV,
        settings.BROKER,
        settings.DRY_RUN,
    )
    init_db()
    logger.info("Database ready at %s", settings.DB_PATH)

    # Start watcher scheduler
    from watcher.scheduler import WatcherScheduler
    from watcher.calendar_guard import CalendarGuard
    guard = CalendarGuard()

    # Start position watcher if broker is configured
    from broker_core.factory import get_broker
    from broker_client.order_router import OrderRouter
    from broker_client.position_manager import PositionManager
    from broker_client.position_watcher import PositionWatcher

    broker = get_broker()
    router = OrderRouter(broker=broker)
    position_manager = PositionManager()
    watcher = PositionWatcher(broker=broker, router=router, position_manager=position_manager)
    watcher.start()
    logger.info("PositionWatcher started")

    scheduler = WatcherScheduler(calendar=guard)

    from alerts.alert_engine import AlertEngine
    from signals.signal_fusion import SignalFusion
    fusion = SignalFusion()
    alert_engine = AlertEngine()
    fusion.warmup()  # pre-load FinBERT once at startup (if NEWS_API_KEY set)

    def phase_tick(phase: str) -> None:
        """Run one watcher tick for the active market phase: compute + route."""
        logger.info(
            "Watcher tick | phase=%s | market_open=%s",
            phase, guard.is_market_open(),
        )
        try:
            state = fusion.compute(phase=phase)
            logger.info(
                "Signal computed | direction=%s | confidence=%d | composite=%d",
                state.current_regime, state.confidence, state.composite_score,
            )
            # Event-driven, high-priority signals (macro release, geopolitical
            # shock) route straight to Discord #alerts — bypassing the composite
            # MEAN, which would otherwise dilute a single hot signal below the
            # alert threshold. send_critical dedups by content for 30 min.
            event_fired = False
            for s in state.signals_active:
                if s.source == "macro" and s.confidence >= settings.CONFIDENCE_HIGH:
                    alert_engine.send_critical(_macro_alert_msg(s))
                    event_fired = True
                elif s.metadata.get("geopolitical_shock"):
                    alert_engine.send_critical(_geo_alert_msg(s))
                    event_fired = True
            if not event_fired and state.confidence >= settings.CONFIDENCE_HIGH:
                alert_engine.send_signal_alert(state)
        except Exception as exc:  # noqa: BLE001 — a tick must never crash the loop
            logger.error("Signal computation failed: %s", exc)

    def geopolitical_tick() -> None:
        """Dedicated sub-minute geopolitical poll → Discord #alerts on shock.

        Runs independently of the routine (up-to-5-min) phase tick so a breaking
        event (e.g. military strikes) is caught within ~1 minute. Reuses the
        cached FinBERT scorer; RSS is the real-time source (NewsAPI free tier is
        delayed/rate-limited).
        """
        if not settings.GEOPOLITICAL_WATCH:
            return
        scorer = fusion.get_sentiment_scorer()
        if scorer is None:
            return
        shock = scorer.check_geopolitical()  # type: ignore[attr-defined]
        if shock is not None:
            logger.warning("Geopolitical poll: SHOCK → routing to Discord #alerts")
            alert_engine.send_critical(_geo_alert_msg(shock))

    def eod_report() -> None:
        logger.info("EOD report job fired (16:05 ET)")

    # Register all phase jobs up front; whatever phase the market is already in
    # fires immediately (no waiting for the next scheduled transition).
    scheduler.register_default_jobs(phase_tick, on_eod=eod_report)
    if settings.GEOPOLITICAL_WATCH:
        # Cross-phase sub-minute poll: active whenever the market window is
        # open (overnight through power_hour), idle only when fully closed.
        scheduler.add_interval_job(
            "geopolitical",
            geopolitical_tick,
            seconds=settings.GEOPOLITICAL_POLL_SECONDS,
            allowed_phases={
                "overnight", "premarket", "macro", "open", "session", "power_hour",
            },
        )
    scheduler.start()
    logger.info("WatcherScheduler started — running phases per market calendar")

    logger.info("Startup complete. System is live.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutdown requested — stopping scheduler and watcher.")
        scheduler.stop()
        watcher.stop()
        logger.info("Clean shutdown complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutdown requested — exiting cleanly.")
        sys.exit(0)
    except Exception:
        logger.critical("Fatal startup error — see log for details.", exc_info=True)
        sys.exit(1)
