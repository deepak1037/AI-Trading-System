"""APScheduler phase-aware job scheduler (Day 5).

Runs all watcher phases on their configured intervals (Section 12 of CLAUDE.md).
Phase intervals are always read from settings — never hardcoded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from config.settings import settings
from core.logger import get_logger
from watcher.calendar_guard import CalendarGuard

logger = get_logger(__name__)


class WatcherScheduler:
    """Configures and runs the phase-aware APScheduler.

    Each phase has its own interval (from config). The scheduler checks
    CalendarGuard before running any job — no work on holidays or weekends.
    """

    def __init__(self, calendar: Optional[CalendarGuard] = None) -> None:
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._calendar = calendar or CalendarGuard()
        self._jobs: dict[str, Any] = {}

    def _guarded(self, phase: str, fn: Callable) -> Callable:
        """Wrap a job function with calendar guard + exception catching."""
        def wrapper():
            if not self._calendar.is_trading_day():
                logger.debug("Scheduler[%s]: not a trading day — skipping", phase)
                return
            try:
                fn()
            except Exception as exc:
                logger.error("Scheduler[%s]: job failed: %s", phase, exc)
        wrapper.__name__ = f"guarded_{phase}"
        return wrapper

    def add_overnight_job(self, fn: Callable) -> None:
        """Overnight scan: every WATCHER_OVERNIGHT_INTERVAL minutes."""
        job = self._scheduler.add_job(
            self._guarded("overnight", fn),
            IntervalTrigger(minutes=settings.WATCHER_OVERNIGHT_INTERVAL),
            id="overnight",
            replace_existing=True,
        )
        self._jobs["overnight"] = job
        logger.info("Scheduler: overnight job added (every %dm)", settings.WATCHER_OVERNIGHT_INTERVAL)

    def add_premarket_job(self, fn: Callable) -> None:
        """Pre-market ramp: every WATCHER_PREMARKET_INTERVAL minutes."""
        job = self._scheduler.add_job(
            self._guarded("premarket", fn),
            IntervalTrigger(minutes=settings.WATCHER_PREMARKET_INTERVAL),
            id="premarket",
            replace_existing=True,
        )
        self._jobs["premarket"] = job
        logger.info("Scheduler: premarket job added (every %dm)", settings.WATCHER_PREMARKET_INTERVAL)

    def add_macro_job(self, fn: Callable) -> None:
        """Macro window: every WATCHER_MACRO_INTERVAL seconds."""
        job = self._scheduler.add_job(
            self._guarded("macro", fn),
            IntervalTrigger(seconds=settings.WATCHER_MACRO_INTERVAL),
            id="macro",
            replace_existing=True,
        )
        self._jobs["macro"] = job
        logger.info("Scheduler: macro job added (every %ds)", settings.WATCHER_MACRO_INTERVAL)

    def add_open_job(self, fn: Callable) -> None:
        """Market open first 30m: every WATCHER_OPEN_INTERVAL minutes."""
        job = self._scheduler.add_job(
            self._guarded("open", fn),
            IntervalTrigger(minutes=settings.WATCHER_OPEN_INTERVAL),
            id="open",
            replace_existing=True,
        )
        self._jobs["open"] = job
        logger.info("Scheduler: open job added (every %dm)", settings.WATCHER_OPEN_INTERVAL)

    def add_session_job(self, fn: Callable) -> None:
        """Mid-session: every WATCHER_SESSION_INTERVAL minutes."""
        job = self._scheduler.add_job(
            self._guarded("session", fn),
            IntervalTrigger(minutes=settings.WATCHER_SESSION_INTERVAL),
            id="session",
            replace_existing=True,
        )
        self._jobs["session"] = job
        logger.info("Scheduler: session job added (every %dm)", settings.WATCHER_SESSION_INTERVAL)

    def add_power_hour_job(self, fn: Callable) -> None:
        """Power hour: every WATCHER_POWER_HOUR_INTERVAL minutes."""
        job = self._scheduler.add_job(
            self._guarded("power_hour", fn),
            IntervalTrigger(minutes=settings.WATCHER_POWER_HOUR_INTERVAL),
            id="power_hour",
            replace_existing=True,
        )
        self._jobs["power_hour"] = job
        logger.info("Scheduler: power_hour job added (every %dm)", settings.WATCHER_POWER_HOUR_INTERVAL)

    def add_eod_job(self, fn: Callable, hour: int = 16, minute: int = 5) -> None:
        """EOD report: once daily at 4:05 PM ET."""
        from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

        job = self._scheduler.add_job(
            self._guarded("eod", fn),
            CronTrigger(hour=hour, minute=minute, timezone="America/New_York"),
            id="eod",
            replace_existing=True,
        )
        self._jobs["eod"] = job
        logger.info("Scheduler: eod job added (daily %02d:%02d ET)", hour, minute)

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("WatcherScheduler started with %d job(s)", len(self._jobs))

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("WatcherScheduler stopped")

    def is_running(self) -> bool:
        return bool(self._scheduler.running)

    def get_jobs(self) -> list[str]:
        return list(self._jobs.keys())


__all__ = ["WatcherScheduler"]
