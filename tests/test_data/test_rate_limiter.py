"""Tests for data.rate_limiter — token-bucket rate limiter."""

from __future__ import annotations

import threading

import pytest

from data.rate_limiter import RateLimiter


def _make_limiter(rate: float = 10.0) -> tuple[RateLimiter, list[float]]:
    """Return a RateLimiter with an injected sleep spy and a fast clock."""
    slept: list[float] = []
    clock_state = {"t": 0.0}

    def clock() -> float:
        return clock_state["t"]

    def advance_and_sleep(secs: float) -> None:
        slept.append(secs)
        clock_state["t"] += secs

    lim = RateLimiter(rate, sleep=advance_and_sleep, time_func=clock)
    return lim, slept


def test_first_acquire_does_not_sleep() -> None:
    lim, slept = _make_limiter(rate=1.0)
    lim.acquire()
    assert slept == []


def test_rapid_consecutive_acquires_sleep_after_burst() -> None:
    lim, slept = _make_limiter(rate=2.0)
    # Bucket starts full (2 tokens). First two calls free; third sleeps.
    lim.acquire()
    lim.acquire()
    lim.acquire()  # bucket empty → must sleep
    assert len(slept) == 1
    assert slept[0] > 0


def test_sleep_duration_matches_refill_rate() -> None:
    lim, slept = _make_limiter(rate=4.0)  # 0.25s per token
    # Drain the bucket.
    for _ in range(4):
        lim.acquire()
    # Next acquire should sleep for ~0.25s.
    lim.acquire()
    assert len(slept) >= 1
    assert pytest.approx(slept[-1], abs=0.05) == 0.25


def test_rate_property_returns_configured_rate() -> None:
    lim = RateLimiter(5.0)
    assert lim.rate == 5.0


def test_invalid_rate_raises() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0.0)
    with pytest.raises(ValueError):
        RateLimiter(-1.0)


def test_thread_safety_no_exceptions() -> None:
    """Multiple threads acquiring simultaneously must not raise."""
    lim = RateLimiter(100.0)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(5):
                lim.acquire()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []
