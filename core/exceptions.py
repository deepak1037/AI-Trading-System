"""Custom exception hierarchy for the AI-Trading-System.

See CLAUDE.md Section 3.4. Every exception carries a ``severity`` and an
arbitrary ``context`` dict so it can be logged with full detail before being
raised. ``CRITICAL`` severity is the trigger AlertEngine uses to fire an SMS.

Hierarchy::

    TradingSystemError
    ├── ConfigError          # bad config at startup
    ├── BrokerError          # broker API failure
    │   └── OrderRejected    # order rejected by broker
    ├── DataError            # data feed failure
    ├── SignalError          # signal computation failure
    └── RiskError            # risk limit breach (CRITICAL — triggers SMS)
"""

from __future__ import annotations

from typing import Any, Literal

Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class TradingSystemError(Exception):
    """Base class for every error raised inside the trading system.

    Args:
        message: Human-readable description of what went wrong.
        severity: One of ``LOW``, ``MEDIUM``, ``HIGH``, ``CRITICAL``. A
            ``CRITICAL`` severity is what AlertEngine watches for to send an
            immediate SMS.
        **context: Any additional structured context (order, response, ticker,
            …). Stored on the instance and included in ``__str__``.
    """

    #: Default severity for the class; subclasses may override.
    default_severity: Severity = "MEDIUM"

    def __init__(
        self,
        message: str,
        severity: Severity | None = None,
        **context: Any,
    ) -> None:
        self.message = message
        self.severity: Severity = severity or self.default_severity
        self.context: dict[str, Any] = context
        super().__init__(message)

    @property
    def is_critical(self) -> bool:
        """True when this error must trigger an immediate alert."""
        return self.severity == "CRITICAL"

    def __str__(self) -> str:
        base = f"[{self.severity}] {self.message}"
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{base} ({ctx})"
        return base

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.message!r}, "
            f"severity={self.severity!r}, context={self.context!r})"
        )


class ConfigError(TradingSystemError):
    """Raised when configuration is missing, malformed, or invalid at startup.

    Always fatal — the system must fail fast rather than run with bad config.
    """

    default_severity: Severity = "CRITICAL"


class BrokerError(TradingSystemError):
    """Raised when a broker API call fails (network, auth, 5xx, etc.)."""

    default_severity: Severity = "HIGH"


class OrderRejected(BrokerError):
    """Raised when the broker explicitly rejects an order."""

    default_severity: Severity = "HIGH"


class DataError(TradingSystemError):
    """Raised when a data feed (FRED, news, market data, …) fails."""

    default_severity: Severity = "MEDIUM"


class SignalError(TradingSystemError):
    """Raised when a signal computation fails."""

    default_severity: Severity = "MEDIUM"


class RiskError(TradingSystemError):
    """Raised when a risk limit is breached.

    Always ``CRITICAL`` — this triggers an immediate SMS via AlertEngine and
    typically halts the trading engine.
    """

    default_severity: Severity = "CRITICAL"


__all__ = [
    "Severity",
    "TradingSystemError",
    "ConfigError",
    "BrokerError",
    "OrderRejected",
    "DataError",
    "SignalError",
    "RiskError",
]
