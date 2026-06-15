"""Tests for broker_client/risk/dividend_checker.py."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from broker_client.risk.dividend_checker import DividendAlert, DividendChecker


def _put(ticker: str, strike: float, expiry: str, entry_price: float = 2.0, underlying: float = 150.0):
    return {
        "ticker": ticker,
        "option_type": "put",
        "position_type": "options_short",
        "strike": strike,
        "expiry": expiry,
        "qty": 1,
        "entry_price": entry_price,
        "underlying_price": underlying,
    }


def _mock_yf(ticker: str, ex_div: date | None, dividend: float, price: float):
    """Patch DividendChecker static methods for offline tests."""
    return {
        "_get_ex_dividend_date": staticmethod(lambda t: ex_div if t == ticker else None),
        "_get_dividend_amount": staticmethod(lambda t: dividend if t == ticker else 0.0),
        "_get_price": staticmethod(lambda t, p: price),
    }


def test_alerts_when_dividend_threatens_put(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "DIVIDEND_RISK_THRESHOLD_PCT", 0.5)

    checker = DividendChecker()
    expiry = (date.today() + timedelta(days=30)).isoformat()
    ex_div = date.today() + timedelta(days=10)  # before expiry

    monkeypatch.setattr(DividendChecker, "_get_ex_dividend_date", staticmethod(lambda t: ex_div))
    # Stock at $100, put strike $95 → OTM buffer = 5%
    # Dividend = $4 → dividend_pct = 4% → > 50% of 5% buffer (threshold=0.5) → alert
    monkeypatch.setattr(DividendChecker, "_get_dividend_amount", staticmethod(lambda t: 4.0))
    monkeypatch.setattr(DividendChecker, "_get_price", staticmethod(lambda t, p: 100.0))

    pos = _put("JNJ", 95.0, expiry, underlying=100.0)
    alerts = checker.check_dividend_risk([pos])
    assert len(alerts) == 1
    assert alerts[0].ticker == "JNJ"


def test_no_alert_when_dividend_small(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "DIVIDEND_RISK_THRESHOLD_PCT", 0.5)

    checker = DividendChecker()
    expiry = (date.today() + timedelta(days=30)).isoformat()
    ex_div = date.today() + timedelta(days=10)

    monkeypatch.setattr(DividendChecker, "_get_ex_dividend_date", staticmethod(lambda t: ex_div))
    # Stock at $100, put strike $80 → OTM buffer = 20%
    # Dividend = $0.25 → dividend_pct = 0.25% → 0.25/20 = 1.25% < 50% → no alert
    monkeypatch.setattr(DividendChecker, "_get_dividend_amount", staticmethod(lambda t: 0.25))
    monkeypatch.setattr(DividendChecker, "_get_price", staticmethod(lambda t, p: 100.0))

    pos = _put("JNJ", 80.0, expiry, underlying=100.0)
    alerts = checker.check_dividend_risk([pos])
    assert len(alerts) == 0


def test_no_alert_when_ex_div_after_expiry(monkeypatch):
    checker = DividendChecker()
    expiry = (date.today() + timedelta(days=10)).isoformat()
    ex_div = date.today() + timedelta(days=20)  # AFTER expiry

    monkeypatch.setattr(DividendChecker, "_get_ex_dividend_date", staticmethod(lambda t: ex_div))
    monkeypatch.setattr(DividendChecker, "_get_dividend_amount", staticmethod(lambda t: 3.0))
    monkeypatch.setattr(DividendChecker, "_get_price", staticmethod(lambda t, p: 100.0))

    pos = _put("JNJ", 95.0, expiry, underlying=100.0)
    alerts = checker.check_dividend_risk([pos])
    assert len(alerts) == 0


def test_skip_call_positions():
    checker = DividendChecker()
    call_pos = {
        "ticker": "AAPL", "option_type": "call", "position_type": "options_short",
        "strike": 200.0, "expiry": "2025-12-19", "qty": 1, "entry_price": 5.0,
    }
    alerts = checker.check_dividend_risk([call_pos])
    assert len(alerts) == 0


def test_put_itm_risk_flag(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "DIVIDEND_RISK_THRESHOLD_PCT", 0.5)

    checker = DividendChecker()
    expiry = (date.today() + timedelta(days=30)).isoformat()
    ex_div = date.today() + timedelta(days=5)

    monkeypatch.setattr(DividendChecker, "_get_ex_dividend_date", staticmethod(lambda t: ex_div))
    # Dividend > OTM buffer → put_itm_risk=True
    monkeypatch.setattr(DividendChecker, "_get_dividend_amount", staticmethod(lambda t: 8.0))
    monkeypatch.setattr(DividendChecker, "_get_price", staticmethod(lambda t, p: 100.0))

    pos = _put("JNJ", 97.0, expiry, underlying=100.0)  # 3% OTM, $8 dividend
    alerts = checker.check_dividend_risk([pos])
    assert len(alerts) == 1
    assert alerts[0].put_itm_risk is True
