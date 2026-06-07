"""Market calendar guard (Day 5).

Checks whether the US equity market is open, handles holidays and early
closes. Uses pandas_market_calendars as the authoritative source.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas_market_calendars as mcal  # type: ignore[import-untyped]

from core.logger import get_logger

logger = get_logger(__name__)

_EXCHANGE = "NYSE"


class CalendarGuard:
    """Answers questions about the trading calendar for a given exchange.

    Wraps pandas_market_calendars so the rest of the system never has
    to know about exchange-specific holiday rules.
    """

    def __init__(self, exchange: str = _EXCHANGE) -> None:
        self._exchange = exchange
        self._cal = mcal.get_calendar(exchange)

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """Return True if the market is open at the given datetime.

        Args:
            dt: UTC datetime to check. Defaults to now.
        """
        dt = dt or datetime.now(tz=timezone.utc)
        start = dt.strftime("%Y-%m-%d")
        end = start
        try:
            schedule = self._cal.schedule(start_date=start, end_date=end)
            if schedule.empty:
                return False
            row = schedule.iloc[0]
            market_open = row["market_open"].to_pydatetime()
            market_close = row["market_close"].to_pydatetime()
            # Ensure timezone-aware comparison
            if market_open.tzinfo is None:
                import pytz  # type: ignore[import-untyped]
                market_open = pytz.utc.localize(market_open)
                market_close = pytz.utc.localize(market_close)
            return bool(market_open <= dt <= market_close)
        except Exception as exc:
            logger.warning("CalendarGuard.is_market_open error: %s", exc)
            return False

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        """Return True if the given date is a trading day (no holiday / weekend)."""
        d = d or date.today()
        ds = d.strftime("%Y-%m-%d")
        try:
            schedule = self._cal.schedule(start_date=ds, end_date=ds)
            return not schedule.empty
        except Exception as exc:
            logger.warning("CalendarGuard.is_trading_day error: %s", exc)
            return False

    def next_trading_day(self, d: Optional[date] = None) -> date:
        """Return the next trading day after d (or today if d is None)."""
        d = d or date.today()
        candidate = d + timedelta(days=1)
        for _ in range(14):  # max 2-week lookout
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def market_hours(self, d: Optional[date] = None) -> Optional[tuple[datetime, datetime]]:
        """Return (market_open, market_close) UTC datetimes for d, or None if closed."""
        d = d or date.today()
        ds = d.strftime("%Y-%m-%d")
        try:
            schedule = self._cal.schedule(start_date=ds, end_date=ds)
            if schedule.empty:
                return None
            row = schedule.iloc[0]
            return (
                row["market_open"].to_pydatetime(),
                row["market_close"].to_pydatetime(),
            )
        except Exception as exc:
            logger.warning("CalendarGuard.market_hours error: %s", exc)
            return None

    def is_premarket(self, dt: Optional[datetime] = None) -> bool:
        """Return True if dt is in pre-market hours (4:00–9:30 AM ET)."""
        import zoneinfo

        dt = dt or datetime.now(tz=timezone.utc)
        et = dt.astimezone(zoneinfo.ZoneInfo("America/New_York"))
        from datetime import time
        return time(4, 0) <= et.time() < time(9, 30) and self.is_trading_day(et.date())

    def is_power_hour(self, dt: Optional[datetime] = None) -> bool:
        """Return True if dt is in power hour (3:00–4:00 PM ET)."""
        import zoneinfo
        from datetime import time

        dt = dt or datetime.now(tz=timezone.utc)
        et = dt.astimezone(zoneinfo.ZoneInfo("America/New_York"))
        return time(15, 0) <= et.time() < time(16, 0) and self.is_trading_day(et.date())


__all__ = ["CalendarGuard"]
