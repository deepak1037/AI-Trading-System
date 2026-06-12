"""Tests for the 21-DTE alert system on PositionWatcher (Step 5)."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from broker_client.order_router import OrderRouter
from broker_client.position_manager import PositionManager
from broker_client.position_watcher import PositionWatcher
from broker_core.base_broker import OrderResult


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "dte.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "account_id TEXT, ticker TEXT, strategy TEXT, position_type TEXT, qty INTEGER, "
            "entry_price REAL, stop_loss REAL, take_profit REAL, opened_at TEXT, "
            "closed_at TEXT, exit_reason TEXT, realized_pnl REAL, is_open INTEGER DEFAULT 1, "
            "bucket INTEGER DEFAULT 1, sub_type TEXT DEFAULT 'msp');"
        )
    return path


@pytest.fixture
def alerts():
    a = MagicMock()
    a.send_alert.return_value = True
    return a


@pytest.fixture
def watcher(db, alerts):
    router = MagicMock(spec=OrderRouter)
    router.execute.return_value = OrderResult(order_id="X", status="filled", fill_price=1.0)
    return PositionWatcher(
        broker=None,
        router=router,
        position_manager=PositionManager(db_path=db),
        alert_engine=alerts,
    )


def _opt(**kw):
    base = {
        "ticker": "HOOD",
        "strategy_name": "cash_secured_put",
        "position_type": "options_short",
        "option_type": "put",
        "strike": 10.0,
        "qty": 1,
        "entry_price": 1.0,
        "current_price": 0.5,
        "dte_remaining": 30,
    }
    base.update(kw)
    return base


class TestDteAlerts:
    def test_21dte_fires_once(self, watcher, alerts):
        watcher.register(_opt(dte_remaining=20))
        watcher.check_dte_alerts()
        # 21 DTE alert (yellow/signals) fired.
        titles = [c.kwargs["title"] for c in alerts.send_alert.call_args_list]
        assert any("21 DTE" in t for t in titles)
        # Second pass: no duplicate 21-DTE alert.
        alerts.send_alert.reset_mock()
        watcher.check_dte_alerts()
        titles2 = [c.kwargs["title"] for c in alerts.send_alert.call_args_list]
        assert not any("21 DTE" in t for t in titles2)

    def test_no_alert_above_21dte(self, watcher, alerts):
        watcher.register(_opt(dte_remaining=45, current_price=0.95))
        watcher.check_dte_alerts()
        titles = [c.kwargs["title"] for c in alerts.send_alert.call_args_list]
        assert not any("21 DTE" in t for t in titles)

    def test_7dte_urgent_fires(self, watcher, alerts):
        watcher.register(_opt(dte_remaining=5, current_price=0.95))
        watcher.check_dte_alerts()
        urgent = [c for c in alerts.send_alert.call_args_list
                  if "URGENT" in c.kwargs["title"]]
        assert urgent
        assert urgent[0].kwargs["channel"] == "alerts"
        assert urgent[0].kwargs["color"] == "red"

    def test_immediate_exit_signal_to_opportunities(self, watcher, alerts):
        # 50% profit on short DTE → IMMEDIATE FULL_EXIT → green opportunities.
        watcher.register(_opt(dte_remaining=10, entry_price=1.0, current_price=0.4))
        watcher.check_dte_alerts()
        exit_calls = [c for c in alerts.send_alert.call_args_list
                      if "EXIT SIGNAL" in c.kwargs["title"]]
        assert exit_calls
        assert exit_calls[0].kwargs["channel"] == "opportunities"

    def test_equity_positions_skipped(self, watcher, alerts):
        # No expiry / dte → not an option → no DTE alert.
        watcher.register({
            "ticker": "AAPL", "strategy_name": "equity_long_short",
            "qty": 10, "entry_price": 150.0, "current_price": 150.0,
        })
        fired = watcher.check_dte_alerts()
        assert fired == []
        alerts.send_alert.assert_not_called()

    def test_returns_recommendations(self, watcher):
        watcher.register(_opt(dte_remaining=20))
        recs = watcher.check_dte_alerts()
        assert recs and recs[0].action

    def test_no_alert_engine_is_noop(self, db):
        router = MagicMock(spec=OrderRouter)
        w = PositionWatcher(broker=None, router=router, position_manager=PositionManager(db_path=db))
        w.register(_opt(dte_remaining=10))
        # Must not raise even with no AlertEngine wired.
        recs = w.check_dte_alerts()
        assert isinstance(recs, list)


class TestSendAlert:
    def test_send_alert_routes_to_channel(self, monkeypatch):
        from alerts.alert_engine import AlertEngine

        engine = AlertEngine()
        captured = {}

        def fake_route(webhook, tag, sev, title, body, color, **kw):
            captured.update(webhook=webhook, tag=tag, color=color, title=title)
            return True

        monkeypatch.setattr(engine, "_discord_route", fake_route)
        ok = engine.send_alert("T", "B", channel="opportunities", color="green")
        assert ok is True
        assert captured["tag"] == "discord_opportunities"

    def test_send_alert_unknown_channel_defaults(self, monkeypatch):
        from alerts.alert_engine import AlertEngine

        engine = AlertEngine()
        captured = {}
        monkeypatch.setattr(
            engine, "_discord_route",
            lambda *a, **k: captured.update(tag=a[1]) or True,
        )
        engine.send_alert("T", "B", channel="nonexistent")
        assert captured["tag"] == "discord_signals"
