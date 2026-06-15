"""Tests for Phase 4 earnings overlap warning in PositionWatcher."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest


class _MockRouter:
    def execute(self, order): ...
    def execute_options(self, order): ...


class _MockPM:
    def get_open_positions(self): return []


def _make_watcher():
    from broker_client.position_watcher import PositionWatcher
    alerts = MagicMock()
    watcher = PositionWatcher(broker=None, router=_MockRouter(), position_manager=_MockPM(),
                               alert_engine=alerts)
    return watcher, alerts


def test_alerts_when_earnings_in_window(monkeypatch):
    watcher, alerts = _make_watcher()
    today = date.today()
    expiry = (today + timedelta(days=30)).isoformat()
    earnings = today + timedelta(days=7)

    watcher.register({"ticker": "AAPL", "expiry": expiry, "bucket": 1, "sub_type": "msp",
                      "qty": 1, "entry_price": 5.0, "position_type": "options_short"})
    monkeypatch.setattr(watcher, "_get_next_earnings", lambda t: earnings)
    watcher.check_earnings_overlaps()
    assert alerts.send_alert.called


def test_no_alert_when_earnings_after_expiry(monkeypatch):
    watcher, alerts = _make_watcher()
    today = date.today()
    expiry = (today + timedelta(days=10)).isoformat()
    earnings = today + timedelta(days=20)  # AFTER expiry

    watcher.register({"ticker": "MSFT", "expiry": expiry, "bucket": 1, "sub_type": "msp",
                      "qty": 1, "entry_price": 5.0, "position_type": "options_short"})
    monkeypatch.setattr(watcher, "_get_next_earnings", lambda t: earnings)
    watcher.check_earnings_overlaps()
    alerts.send_alert.assert_not_called()


def test_critical_alert_when_earnings_3_days(monkeypatch):
    watcher, alerts = _make_watcher()
    today = date.today()
    expiry = (today + timedelta(days=30)).isoformat()
    earnings = today + timedelta(days=2)  # ≤ 3 days → CRITICAL

    watcher.register({"ticker": "NVDA", "expiry": expiry, "bucket": 1, "sub_type": "msp",
                      "qty": 1, "entry_price": 5.0, "position_type": "options_short"})
    monkeypatch.setattr(watcher, "_get_next_earnings", lambda t: earnings)
    watcher.check_earnings_overlaps()

    assert alerts.send_alert.called
    call_kwargs = alerts.send_alert.call_args.kwargs
    assert call_kwargs.get("channel") == "alerts"
    assert call_kwargs.get("color") == "red"


def test_no_alert_without_expiry(monkeypatch):
    watcher, alerts = _make_watcher()
    watcher.register({"ticker": "AMD", "qty": 1, "entry_price": 5.0,
                      "position_type": "options_short"})  # no expiry
    monkeypatch.setattr(watcher, "_get_next_earnings", lambda t: date.today())
    watcher.check_earnings_overlaps()
    alerts.send_alert.assert_not_called()


def test_no_alert_when_no_earnings_found(monkeypatch):
    watcher, alerts = _make_watcher()
    today = date.today()
    expiry = (today + timedelta(days=30)).isoformat()

    watcher.register({"ticker": "CRWD", "expiry": expiry, "bucket": 1, "sub_type": "msp",
                      "qty": 1, "entry_price": 5.0, "position_type": "options_short"})
    monkeypatch.setattr(watcher, "_get_next_earnings", lambda t: None)
    watcher.check_earnings_overlaps()
    alerts.send_alert.assert_not_called()
