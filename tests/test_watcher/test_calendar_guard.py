"""Tests for watcher/calendar_guard.py."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from watcher.calendar_guard import CalendarGuard


@pytest.fixture
def guard():
    return CalendarGuard()


class TestCalendarGuard:
    def test_saturday_not_trading_day(self, guard):
        # 2024-01-06 is a Saturday
        assert guard.is_trading_day(date(2024, 1, 6)) is False

    def test_sunday_not_trading_day(self, guard):
        assert guard.is_trading_day(date(2024, 1, 7)) is False

    def test_regular_weekday_is_trading_day(self, guard):
        # 2024-01-08 is a Monday
        assert guard.is_trading_day(date(2024, 1, 8)) is True

    def test_christmas_2023_not_trading(self, guard):
        # Christmas Day 2023 = Monday Dec 25 — NYSE closed
        assert guard.is_trading_day(date(2023, 12, 25)) is False

    def test_next_trading_day_skips_weekend(self, guard):
        # Friday 2024-01-05 → next trading day is Monday 2024-01-08
        nxt = guard.next_trading_day(date(2024, 1, 5))
        assert nxt == date(2024, 1, 8)

    def test_market_hours_returns_none_on_holiday(self, guard):
        result = guard.market_hours(date(2023, 12, 25))
        assert result is None

    def test_market_hours_returns_tuple_on_trading_day(self, guard):
        result = guard.market_hours(date(2024, 1, 8))
        assert result is not None
        open_, close = result
        assert open_ < close

    def test_is_market_open_false_on_weekend(self, guard):
        # Sunday midnight UTC
        dt = datetime(2024, 1, 7, 0, 0, 0, tzinfo=timezone.utc)
        assert guard.is_market_open(dt) is False
