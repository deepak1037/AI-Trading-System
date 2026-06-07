"""Thread-safe token-bucket rate limiter for external API calls.

Each connector holds its own RateLimiter instance configured from settings.
The bucket starts full, refills at ``calls_per_second``, and caps at that
same burst size. ``acquire()`` blocks only when the bucket is empty.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Token bucket: allows a burst of up to ``calls_per_second`` then throttles.

    Args:
        calls_per_second: Max sustainable call rate (also the burst capacity).
        sleep: Injected sleep function — override in tests for determinism.
        time_func: Injected monotonic clock — override in tests.
    """

    def __init__(
        self,
        calls_per_second: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be > 0")
        self._rate = calls_per_second
        self._tokens = calls_per_second  # start full
        self._last = time_func()
        self._lock = threading.Lock()
        self._sleep = sleep
        self._time = time_func

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        with self._lock:
            now = self._time()
            elapsed = now - self._last
            self._last = now
            # Refill but never exceed capacity.
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
                self._sleep(wait)

    @property
    def rate(self) -> float:
        """Configured calls-per-second rate."""
        return self._rate


__all__ = ["RateLimiter"]
