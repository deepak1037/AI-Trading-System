"""Retry + circuit-breaker decorators (CLAUDE.md Section 3.5).

Every external call (broker, data APIs, alerts) wraps its function with
``@retry`` and ``@circuit_breaker``::

    @retry(max_attempts=settings.API_MAX_RETRIES,
           backoff_seconds=settings.API_BACKOFF_SECONDS,
           exceptions=(BrokerError, DataError))
    @circuit_breaker(failure_threshold=5, recovery_timeout=300)
    def get_options_chain(self, ticker: str) -> OptionsChain: ...

All retry parameters come from config — never hardcoded at call sites. The
decorators are dependency-free and accept injectable ``sleep``/``time_func``
hooks so they can be tested deterministically.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

from core.exceptions import TradingSystemError
from core.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class CircuitBreakerError(TradingSystemError):
    """Raised when a call is attempted while the circuit breaker is OPEN."""

    default_severity = "HIGH"


def retry(
    max_attempts: int,
    backoff_seconds: float,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    *,
    backoff_multiplier: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[F], F]:
    """Retry a function on the given exceptions with exponential backoff.

    Args:
        max_attempts: Total number of attempts (>= 1). The last failure is
            re-raised.
        backoff_seconds: Base delay before the first retry. Each subsequent
            retry waits ``backoff_seconds * backoff_multiplier ** (n - 1)``.
        exceptions: Exception types that trigger a retry. Anything else
            propagates immediately.
        backoff_multiplier: Exponential growth factor between retries.
        sleep: Sleep function, injectable for tests.

    Raises:
        ValueError: If ``max_attempts`` < 1.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempt(s): %s",
                            func.__name__,
                            attempt,
                            exc,
                        )
                        raise
                    delay = backoff_seconds * (backoff_multiplier ** (attempt - 1))
                    logger.warning(
                        "%s attempt %d/%d failed (%s); retrying in %.2fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    sleep(delay)
            # Unreachable: loop either returns or raises. Guard for type checker.
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


class _CircuitState:
    """Mutable state for one circuit breaker. CLOSED → OPEN → HALF_OPEN."""

    __slots__ = ("failures", "opened_at", "state")

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None
        self.state = "CLOSED"  # CLOSED | OPEN | HALF_OPEN


def circuit_breaker(
    failure_threshold: int,
    recovery_timeout: float,
    *,
    expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
    time_func: Callable[[], float] = time.monotonic,
) -> Callable[[F], F]:
    """Trip after ``failure_threshold`` consecutive failures, blocking calls.

    While OPEN, calls fail fast with ``CircuitBreakerError`` until
    ``recovery_timeout`` seconds have elapsed, after which a single trial call
    is allowed (HALF_OPEN). Success closes the circuit; failure re-opens it.

    Args:
        failure_threshold: Consecutive failures before tripping (>= 1).
        recovery_timeout: Seconds the circuit stays OPEN before a trial call.
        expected_exceptions: Exceptions counted as failures.
        time_func: Monotonic clock, injectable for tests.

    Raises:
        ValueError: If ``failure_threshold`` < 1.
    """
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be >= 1")

    # Fallback state for module-level (non-method) functions.
    _global_state = _CircuitState()

    def decorator(func: F) -> F:
        # Per-instance state attr name — stored on the instance __dict__ so
        # each object (e.g. each SchwabBroker) has its own independent circuit.
        _attr = f"_cb_{func.__qualname__.replace('.', '_').replace('<', '').replace('>', '')}"

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve state: per-instance for methods, global for plain functions.
            instance = args[0] if args and hasattr(args[0], "__dict__") else None
            if instance is not None:
                if _attr not in instance.__dict__:
                    instance.__dict__[_attr] = _CircuitState()
                state = instance.__dict__[_attr]
            else:
                state = _global_state

            if state.state == "OPEN":
                assert state.opened_at is not None
                if time_func() - state.opened_at >= recovery_timeout:
                    state.state = "HALF_OPEN"
                    logger.info(
                        "Circuit breaker for %s entering HALF_OPEN", func.__name__
                    )
                else:
                    raise CircuitBreakerError(
                        f"Circuit breaker OPEN for {func.__name__}",
                        failures=state.failures,
                    )

            try:
                result = func(*args, **kwargs)
            except expected_exceptions:
                state.failures += 1
                if state.state == "HALF_OPEN" or state.failures >= failure_threshold:
                    state.state = "OPEN"
                    state.opened_at = time_func()
                    logger.error(
                        "Circuit breaker TRIPPED for %s after %d failure(s)",
                        func.__name__,
                        state.failures,
                    )
                raise
            else:
                if state.state in ("HALF_OPEN", "OPEN"):
                    logger.info("Circuit breaker for %s reset to CLOSED", func.__name__)
                state.failures = 0
                state.opened_at = None
                state.state = "CLOSED"
                return result

        wrapper._circuit_state = _global_state  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator


__all__ = ["retry", "circuit_breaker", "CircuitBreakerError"]
