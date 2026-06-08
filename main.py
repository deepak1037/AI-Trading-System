"""Entry point (CLAUDE.md Section 4): starts watcher + position watcher."""

from __future__ import annotations

import sys
import time

from config.settings import settings
from core.logger import get_logger
from data.db import init_db

logger = get_logger(__name__)


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

    def phase_tick(phase: str) -> None:
        """Run one watcher tick for the active market phase: compute + route."""
        logger.info(
            "Watcher tick | phase=%s | market_open=%s",
            phase, guard.is_market_open(),
        )
        try:
            state = fusion.compute()
            logger.info(
                "Signal computed | direction=%s | confidence=%d | composite=%d",
                state.current_regime, state.confidence, state.composite_score,
            )
            if state.confidence >= settings.CONFIDENCE_HIGH:
                alert_engine.send_signal_alert(state)
        except Exception as exc:  # noqa: BLE001 — a tick must never crash the loop
            logger.error("Signal computation failed: %s", exc)

    def eod_report() -> None:
        logger.info("EOD report job fired (16:05 ET)")

    # Register all phase jobs up front; whatever phase the market is already in
    # fires immediately (no waiting for the next scheduled transition).
    scheduler.register_default_jobs(phase_tick, on_eod=eod_report)
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
