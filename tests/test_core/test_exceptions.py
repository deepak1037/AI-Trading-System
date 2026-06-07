"""Tests for core.exceptions — the custom exception hierarchy."""

from __future__ import annotations

import pytest

from core.exceptions import (
    BrokerError,
    ConfigError,
    DataError,
    OrderRejected,
    RiskError,
    SignalError,
    TradingSystemError,
)


def test_base_defaults() -> None:
    err = TradingSystemError("boom")
    assert err.message == "boom"
    assert err.severity == "MEDIUM"
    assert err.context == {}
    assert err.is_critical is False


def test_severity_override() -> None:
    err = TradingSystemError("boom", severity="LOW")
    assert err.severity == "LOW"


def test_context_captured_and_rendered() -> None:
    err = BrokerError("rejected", order="AMD", code=42)
    assert err.context == {"order": "AMD", "code": 42}
    rendered = str(err)
    assert "rejected" in rendered
    assert "order='AMD'" in rendered
    assert "code=42" in rendered
    assert "[HIGH]" in rendered


def test_hierarchy_is_correct() -> None:
    assert issubclass(ConfigError, TradingSystemError)
    assert issubclass(BrokerError, TradingSystemError)
    assert issubclass(OrderRejected, BrokerError)
    assert issubclass(DataError, TradingSystemError)
    assert issubclass(SignalError, TradingSystemError)
    assert issubclass(RiskError, TradingSystemError)


@pytest.mark.parametrize(
    ("exc_cls", "expected"),
    [
        (ConfigError, "CRITICAL"),
        (BrokerError, "HIGH"),
        (OrderRejected, "HIGH"),
        (DataError, "MEDIUM"),
        (SignalError, "MEDIUM"),
        (RiskError, "CRITICAL"),
    ],
)
def test_default_severities(exc_cls: type[TradingSystemError], expected: str) -> None:
    assert exc_cls("x").severity == expected


def test_risk_and_config_are_critical() -> None:
    assert RiskError("limit breached").is_critical is True
    assert ConfigError("bad config").is_critical is True
    assert BrokerError("api down").is_critical is False


def test_is_raisable_and_catchable_as_base() -> None:
    with pytest.raises(TradingSystemError):
        raise OrderRejected("nope")


def test_repr_round_trip_contains_fields() -> None:
    err = RiskError("breach", limit=0.02)
    text = repr(err)
    assert "RiskError" in text
    assert "breach" in text
    assert "CRITICAL" in text
    assert "limit" in text
