"""Tests for core.retry — retry decorator and circuit breaker."""

from __future__ import annotations

import pytest

from core.exceptions import BrokerError, DataError
from core.retry import CircuitBreakerError, circuit_breaker, retry


class _FakeClock:
    """Deterministic monotonic clock for circuit-breaker tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ── retry ─────────────────────────────────────────────────


def test_retry_returns_on_success_without_sleeping() -> None:
    sleeps: list[float] = []

    @retry(max_attempts=3, backoff_seconds=1.0, sleep=sleeps.append)
    def ok() -> str:
        return "done"

    assert ok() == "done"
    assert sleeps == []


def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    @retry(
        max_attempts=3,
        backoff_seconds=2.0,
        exceptions=(BrokerError,),
        sleep=sleeps.append,
    )
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise BrokerError("transient")
        return "recovered"

    assert flaky() == "recovered"
    assert calls["n"] == 3
    # Exponential backoff: 2.0 * 2**0, then 2.0 * 2**1.
    assert sleeps == [2.0, 4.0]


def test_retry_reraises_after_exhausting_attempts() -> None:
    calls = {"n": 0}

    @retry(
        max_attempts=2,
        backoff_seconds=0.0,
        exceptions=(DataError,),
        sleep=lambda _: None,
    )
    def always_fail() -> None:
        calls["n"] += 1
        raise DataError("down")

    with pytest.raises(DataError):
        always_fail()
    assert calls["n"] == 2


def test_retry_does_not_catch_unlisted_exceptions() -> None:
    calls = {"n": 0}

    @retry(
        max_attempts=3,
        backoff_seconds=0.0,
        exceptions=(BrokerError,),
        sleep=lambda _: None,
    )
    def wrong_error() -> None:
        calls["n"] += 1
        raise ValueError("not retried")

    with pytest.raises(ValueError):
        wrong_error()
    assert calls["n"] == 1  # no retries


def test_retry_rejects_bad_max_attempts() -> None:
    with pytest.raises(ValueError):
        retry(max_attempts=0, backoff_seconds=1.0)


def test_retry_preserves_function_metadata() -> None:
    @retry(max_attempts=1, backoff_seconds=0.0)
    def documented() -> None:
        """A docstring."""

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "A docstring."


# ── circuit_breaker ───────────────────────────────────────


def test_breaker_trips_after_threshold() -> None:
    clock = _FakeClock()

    @circuit_breaker(failure_threshold=3, recovery_timeout=300, time_func=clock)
    def fail() -> None:
        raise BrokerError("api")

    for _ in range(3):
        with pytest.raises(BrokerError):
            fail()

    # Circuit now OPEN — next call fails fast without invoking the function.
    with pytest.raises(CircuitBreakerError):
        fail()


def test_breaker_blocks_until_recovery_timeout() -> None:
    clock = _FakeClock()

    @circuit_breaker(failure_threshold=1, recovery_timeout=300, time_func=clock)
    def fail() -> None:
        raise BrokerError("api")

    with pytest.raises(BrokerError):
        fail()
    with pytest.raises(CircuitBreakerError):
        fail()

    clock.advance(299)
    with pytest.raises(CircuitBreakerError):
        fail()


def test_breaker_half_open_success_closes_circuit() -> None:
    clock = _FakeClock()
    calls = {"n": 0}

    @circuit_breaker(failure_threshold=1, recovery_timeout=100, time_func=clock)
    def sometimes() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerError("first fails")
        return "ok"

    with pytest.raises(BrokerError):
        sometimes()  # trips
    with pytest.raises(CircuitBreakerError):
        sometimes()  # open

    clock.advance(100)  # recovery elapsed -> HALF_OPEN trial
    assert sometimes() == "ok"  # success closes it

    # Closed again: normal calls pass straight through.
    assert sometimes() == "ok"
    assert sometimes._circuit_state.state == "CLOSED"


def test_breaker_half_open_failure_reopens() -> None:
    clock = _FakeClock()

    @circuit_breaker(failure_threshold=1, recovery_timeout=50, time_func=clock)
    def fail() -> None:
        raise BrokerError("still down")

    with pytest.raises(BrokerError):
        fail()
    clock.advance(50)
    # HALF_OPEN trial fails -> reopen immediately.
    with pytest.raises(BrokerError):
        fail()
    with pytest.raises(CircuitBreakerError):
        fail()


def test_breaker_success_resets_failure_count() -> None:
    clock = _FakeClock()
    state = {"fail": True}

    @circuit_breaker(failure_threshold=3, recovery_timeout=10, time_func=clock)
    def toggle() -> str:
        if state["fail"]:
            raise BrokerError("x")
        return "ok"

    # Two failures, then a success should reset the counter below threshold.
    with pytest.raises(BrokerError):
        toggle()
    with pytest.raises(BrokerError):
        toggle()
    state["fail"] = False
    assert toggle() == "ok"
    assert toggle._circuit_state.failures == 0


def test_breaker_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        circuit_breaker(failure_threshold=0, recovery_timeout=10)


def test_retry_and_breaker_compose() -> None:
    """Stacked decorators as shown in CLAUDE.md Section 3.5."""
    clock = _FakeClock()
    calls = {"n": 0}

    @retry(
        max_attempts=5,
        backoff_seconds=0.0,
        exceptions=(BrokerError,),
        sleep=lambda _: None,
    )
    @circuit_breaker(failure_threshold=2, recovery_timeout=300, time_func=clock)
    def call() -> None:
        calls["n"] += 1
        raise BrokerError("down")

    # retry drives calls; breaker trips after 2 real failures, then the retry
    # loop sees CircuitBreakerError (not in its exception list) and propagates.
    with pytest.raises(CircuitBreakerError):
        call()
    assert calls["n"] == 2
