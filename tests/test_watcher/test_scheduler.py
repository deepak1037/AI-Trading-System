"""Tests for watcher/scheduler.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from watcher.scheduler import WatcherScheduler


@pytest.fixture
def scheduler():
    mock_cal = MagicMock()
    mock_cal.is_trading_day.return_value = True
    sched = WatcherScheduler(calendar=mock_cal)
    yield sched
    if sched.is_running():
        sched.stop()


class TestWatcherScheduler:
    def test_starts_and_stops(self, scheduler):
        scheduler.start()
        assert scheduler.is_running()
        scheduler.stop()
        assert not scheduler.is_running()

    def test_add_overnight_job(self, scheduler):
        fn = MagicMock()
        scheduler.add_overnight_job(fn)
        assert "overnight" in scheduler.get_jobs()

    def test_add_all_phases(self, scheduler):
        for method in [
            scheduler.add_overnight_job,
            scheduler.add_premarket_job,
            scheduler.add_macro_job,
            scheduler.add_open_job,
            scheduler.add_session_job,
            scheduler.add_power_hour_job,
        ]:
            method(MagicMock())
        jobs = scheduler.get_jobs()
        assert "overnight" in jobs
        assert "premarket" in jobs
        assert "macro" in jobs
        assert "open" in jobs
        assert "session" in jobs
        assert "power_hour" in jobs

    def test_add_eod_job(self, scheduler):
        scheduler.add_eod_job(MagicMock())
        assert "eod" in scheduler.get_jobs()

    def test_not_running_initially(self, scheduler):
        assert not scheduler.is_running()

    def test_guarded_skips_non_trading_day(self, scheduler):
        scheduler._calendar.is_trading_day.return_value = False
        fn = MagicMock()
        guarded = scheduler._guarded("test", fn)
        guarded()
        fn.assert_not_called()

    def test_guarded_calls_fn_on_trading_day(self, scheduler):
        fn = MagicMock()
        guarded = scheduler._guarded("test", fn)
        guarded()
        fn.assert_called_once()

    def test_guarded_catches_exceptions(self, scheduler):
        def bad_fn():
            raise RuntimeError("boom")
        guarded = scheduler._guarded("test", bad_fn)
        guarded()  # Should not raise
