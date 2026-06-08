"""APScheduler phase-aware job scheduler (Day 5).

Runs all watcher phases on their configured intervals (Section 12 of CLAUDE.md).
Phase intervals are always read from settings — never hardcoded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from config.settings import settings
from core.logger import get_logger
from watcher.calendar_guard import CalendarGuard

logger = get_logger(__name__)

# Phase → IntervalTrigger factory (intervals always from settings).
_PHASE_TRIGGERS: dict[str, Callable[[], IntervalTrigger]] = {
    "overnight": lambda: IntervalTrigger(minutes=settings.WATCHER_OVERNIGHT_INTERVAL),
    "premarket": lambda: IntervalTrigger(minutes=settings.WATCHER_PREMARKET_INTERVAL),
    "macro": lambda: IntervalTrigger(seconds=settings.WATCHER_MACRO_INTERVAL),
    "open": lambda: IntervalTrigger(minutes=settings.WATCHER_OPEN_INTERVAL),
    "session": lambda: IntervalTrigger(minutes=settings.WATCHER_SESSION_INTERVAL),
    "power_hour": lambda: IntervalTrigger(minutes=settings.WATCHER_POWER_HOUR_INTERVAL),
}


class WatcherScheduler:
    """Configures and runs the phase-aware APScheduler.

    Each phase has its own interval (from config). The scheduler checks
    CalendarGuard before running any job — no work on holidays or weekends.
    """

    def __init__(self, calendar: Optional[CalendarGuard] = None) -> None:
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._calendar = calendar or CalendarGuard()
        self._jobs: dict[str, Any] = {}

    def _guarded(self, phase: str, fn: Callable, phase_gated: bool = False) -> Callable:
        """Wrap a job function with calendar guard + exception catching.

        Args:
            phase_gated: When True, the job only runs while ``phase`` is the
                current market phase (so the every-N-minutes session job doesn't
                fire overnight). Off for the bare guard and for the EOD cron.
        """
        def wrapper():
            if not self._calendar.is_trading_day():
                logger.debug("Scheduler[%s]: not a trading day — skipping", phase)
                return
            if phase_gated:
                current = self._calendar.current_phase()
                if current != phase:
                    logger.debug(
                        "Scheduler[%s]: current phase is %s — skipping", phase, current
                    )
                    return
            try:
                fn()
            except Exception as exc:
                logger.error("Scheduler[%s]: job failed: %s", phase, exc)
        wrapper.__name__ = f"guarded_{phase}"
        return wrapper

    def _add_phase_job(self, phase: str, fn: Callable, fire_now: bool = False) -> None:
        """Register an interval job for a phase, gated to that phase's window.

        When ``fire_now`` is True the job's first run is scheduled for *now*
        (used at startup for whatever phase the market is already in), instead
        of waiting a full interval for the trigger's first fire.
        """
        kwargs: dict[str, Any] = {}
        if fire_now:
            kwargs["next_run_time"] = datetime.now(tz=timezone.utc)
        job = self._scheduler.add_job(
            self._guarded(phase, fn, phase_gated=True),
            _PHASE_TRIGGERS[phase](),
            id=phase,
            replace_existing=True,
            **kwargs,
        )
        self._jobs[phase] = job
        logger.info("Scheduler: %s job added%s", phase, " (firing now)" if fire_now else "")

    def add_overnight_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("overnight", fn, fire_now)

    def add_premarket_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("premarket", fn, fire_now)

    def add_macro_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("macro", fn, fire_now)

    def add_open_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("open", fn, fire_now)

    def add_session_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("session", fn, fire_now)

    def add_power_hour_job(self, fn: Callable, fire_now: bool = False) -> None:
        self._add_phase_job("power_hour", fn, fire_now)

    def add_eod_job(self, fn: Callable, hour: int = 16, minute: int = 5) -> None:
        """EOD report: once daily at 4:05 PM ET (cron — not phase-gated)."""
        from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

        job = self._scheduler.add_job(
            self._guarded("eod", fn, phase_gated=False),
            CronTrigger(hour=hour, minute=minute, timezone="America/New_York"),
            id="eod",
            replace_existing=True,
        )
        self._jobs["eod"] = job
        logger.info("Scheduler: eod job added (daily %02d:%02d ET)", hour, minute)

    def register_default_jobs(
        self, tick: Callable[[str], None], on_eod: Optional[Callable] = None
    ) -> None:
        """Register every phase job up front; the CURRENT phase fires immediately.

        ``tick`` is called with the phase name each time a phase job runs. This
        is the fix for "0 jobs at startup" — all phases are registered now, and
        whatever phase the market is already in starts running without waiting
        for the next scheduled transition.
        """
        current = self._calendar.current_phase()
        adders = {
            "overnight": self.add_overnight_job,
            "premarket": self.add_premarket_job,
            "macro": self.add_macro_job,
            "open": self.add_open_job,
            "session": self.add_session_job,
            "power_hour": self.add_power_hour_job,
        }
        for phase, adder in adders.items():
            adder(lambda p=phase: tick(p), fire_now=(phase == current))
        if on_eod is not None:
            self.add_eod_job(on_eod)
        logger.info(
            "Registered %d phase job(s); current phase=%s (fires immediately)",
            len(self._jobs), current,
        )

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            phase = self._calendar.current_phase()
            logger.info(
                "WatcherScheduler started with %d job(s) | current phase=%s",
                len(self._jobs), phase,
            )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("WatcherScheduler stopped")

    def is_running(self) -> bool:
        return bool(self._scheduler.running)

    def get_jobs(self) -> list[str]:
        return list(self._jobs.keys())


__all__ = ["WatcherScheduler"]
