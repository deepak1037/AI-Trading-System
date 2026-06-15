"""Tests for broker_client/risk/concentration_checker.py."""

from __future__ import annotations

import pytest

from broker_client.risk.concentration_checker import ConcentrationAlert, ConcentrationChecker


def _pos(ticker: str, pct_of_100k: float, expiry: str | None = None, sector: str | None = None):
    """Build a minimal position dict with optional expiry."""
    # entry_price × qty × 100 = pct_of_100k% of 100k
    val = 100_000 * pct_of_100k / 100
    p = {
        "ticker": ticker,
        "option_type": "put",
        "position_type": "options_short",
        "qty": 1,
        "entry_price": val / 100,  # ÷100 for contract multiplier
    }
    if expiry:
        p["expiry"] = expiry
    return p


# ── sector ─────────────────────────────────────────────────────────────────────

def test_sector_warning_fires(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 30.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 60.0)
    # NVDA (Semiconductors) = 40% of portfolio
    positions = [
        _pos("NVDA", 40),
        _pos("JNJ", 30),
        _pos("VLO", 30),
    ]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    sector_alerts = [a for a in alerts if a.alert_type == "SECTOR" and a.severity == "WARNING"]
    assert len(sector_alerts) >= 1
    assert any("Semiconductors" in a.message for a in sector_alerts)


def test_sector_critical_fires(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 30.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 50.0)
    positions = [_pos("NVDA", 80), _pos("JNJ", 20)]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    critical = [a for a in alerts if a.alert_type == "SECTOR" and a.severity == "CRITICAL"]
    assert len(critical) >= 1


def test_no_sector_alert_when_healthy(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 40.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 60.0)
    tickers = ["NVDA", "JNJ", "VLO", "JPM", "CAT"]
    positions = [_pos(t, 20) for t in tickers]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    sector_alerts = [a for a in alerts if a.alert_type == "SECTOR"]
    assert len(sector_alerts) == 0


# ── ticker ─────────────────────────────────────────────────────────────────────

def test_single_ticker_concentration(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SINGLE_TICKER_WARNING_PCT", 10.0)
    positions = [_pos("NVDA", 50), _pos("JNJ", 50)]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    ticker_alerts = [a for a in alerts if a.alert_type == "TICKER"]
    assert any("NVDA" in a.message for a in ticker_alerts)


# ── expiry ─────────────────────────────────────────────────────────────────────

def test_same_expiry_warning(monkeypatch):
    from datetime import date, timedelta
    from config.settings import settings
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 3)
    future_expiry = (date.today() + timedelta(days=14)).isoformat()
    positions = [_pos(t, 20, expiry=future_expiry) for t in ["NVDA", "AAPL", "MSFT"]]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    expiry_alerts = [a for a in alerts if a.alert_type == "EXPIRY"]
    assert len(expiry_alerts) >= 1


def test_no_expiry_alert_spread_dates(monkeypatch):
    from datetime import date, timedelta
    from config.settings import settings
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 5)
    positions = [
        _pos("NVDA", 25, expiry=(date.today() + timedelta(days=14)).isoformat()),
        _pos("AAPL", 25, expiry=(date.today() + timedelta(days=45)).isoformat()),
        _pos("MSFT", 25, expiry=(date.today() + timedelta(days=75)).isoformat()),
        _pos("JNJ", 25, expiry=(date.today() + timedelta(days=110)).isoformat()),
    ]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    expiry_alerts = [a for a in alerts if a.alert_type == "EXPIRY"]
    assert len(expiry_alerts) == 0


# ── cash ───────────────────────────────────────────────────────────────────────

def test_cash_buffer_warning(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 20.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 10.0)
    # Simulate a paper account with low cash
    class _FakePaper:
        class _State:
            equity = 100_000.0
            cash = 12_000.0  # 12% < 20% warning
        def get_state(self): return self._State()

    checker = ConcentrationChecker(paper_account=_FakePaper())
    alerts = checker.check([], portfolio_value=100_000)
    cash_alerts = [a for a in alerts if a.alert_type == "CASH"]
    assert len(cash_alerts) == 1
    assert cash_alerts[0].severity == "WARNING"


def test_cash_buffer_critical(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 20.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 10.0)

    class _FakePaper:
        class _State:
            equity = 100_000.0
            cash = 5_000.0  # 5% < 10% critical
        def get_state(self): return self._State()

    checker = ConcentrationChecker(paper_account=_FakePaper())
    alerts = checker.check([], portfolio_value=100_000)
    cash_alerts = [a for a in alerts if a.alert_type == "CASH"]
    assert cash_alerts[0].severity == "CRITICAL"


def test_no_alerts_on_healthy_portfolio(monkeypatch):
    from datetime import date, timedelta
    from config.settings import settings
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 40.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 60.0)
    monkeypatch.setattr(settings, "SINGLE_TICKER_WARNING_PCT", 30.0)  # each position ≈ 25%
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 5)
    # Disable cash alerts — no paper account, settings-based cash_buffer = varies
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)

    positions = [
        _pos("NVDA", 20, expiry=(date.today() + timedelta(days=14)).isoformat()),
        _pos("JNJ",  20, expiry=(date.today() + timedelta(days=45)).isoformat()),
        _pos("VLO",  20, expiry=(date.today() + timedelta(days=75)).isoformat()),
        _pos("JPM",  20, expiry=(date.today() + timedelta(days=110)).isoformat()),
    ]
    checker = ConcentrationChecker()
    alerts = checker.check(positions, portfolio_value=100_000)
    # Each in a different sector ≈ 25% each — all < 40% sector threshold
    # Each ticker ≈ 25% — below the 30% single-ticker threshold
    assert len(alerts) == 0
