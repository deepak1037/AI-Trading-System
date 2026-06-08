"""Tests for alerts/alert_engine.py — mocks all channels."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from alerts.alert_engine import AlertEngine
from core.exceptions import BrokerError, RiskError


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE alerts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                channel TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )"""
        )
    return AlertEngine(db_path=str(db))


class TestAlertEngineDedup:
    def test_dedup_suppresses_duplicate_within_window(self, engine, mocker):
        mocker.patch.object(engine, "_send_sms", return_value=True)
        engine.send_critical("test alert 1")
        result = engine.send_critical("test alert 1")  # duplicate
        assert result is False

    def test_bypass_dedup_for_risk_errors(self, engine, mocker):
        mocker.patch.object(engine, "_send_sms", return_value=True)
        engine.send_critical("risk breach", bypass_dedup=True)
        result = engine.send_critical("risk breach", bypass_dedup=True)
        assert result is True  # bypass = always send

    def test_different_messages_not_deduped(self, engine, mocker):
        mocker.patch.object(engine, "_send_sms", return_value=True)
        r1 = engine.send_critical("alert A")
        r2 = engine.send_critical("alert B")
        assert r1 is True
        assert r2 is True


class TestAlertEngineSMS:
    def test_sms_not_configured_returns_false(self, engine):
        result = engine._send_sms("test")
        assert result is False  # no Twilio config in test env

    def test_sms_sends_when_configured(self, engine, mocker):
        mocker.patch("config.settings.settings.TWILIO_ACCOUNT_SID", "ACtest")
        mocker.patch("config.settings.settings.TWILIO_AUTH_TOKEN", "token")
        mocker.patch("config.settings.settings.TWILIO_FROM_NUMBER", "+1111")
        mocker.patch("config.settings.settings.TWILIO_TO_NUMBER", "+2222")
        mock_client = MagicMock()
        with patch("twilio.rest.Client", return_value=mock_client):
            result = engine._send_sms("test message")
        assert result is True


class TestAlertEngineSlack:
    def test_slack_not_configured_returns_false(self, engine):
        result = engine._send_slack("test")
        assert result is False


class TestAlertEngineSendGrid:
    def test_sendgrid_not_configured_returns_false(self, engine):
        result = engine._send_email("subject", "body")
        assert result is False


class TestAlertEngineDiscord:
    def test_send_discord_returns_true_on_204(self, engine, mocker):
        resp = MagicMock(status_code=204)
        post = mocker.patch("requests.post", return_value=resp)
        ok = engine._send_discord(
            "https://discord.test/webhook",
            engine._discord_message("T", "body", 65280, [{"name": "a", "value": "b"}]),
        )
        assert ok is True
        # Verify the embed payload shape.
        payload = post.call_args.kwargs["json"]
        embed = payload["embeds"][0]
        assert embed["title"] == "T"
        assert embed["description"] == "body"
        assert embed["color"] == 65280
        assert embed["fields"] == [{"name": "a", "value": "b"}]
        assert "footer" in embed

    def test_send_discord_non_204_returns_false(self, engine, mocker):
        mocker.patch("requests.post", return_value=MagicMock(status_code=400, text="bad"))
        assert engine._send_discord("https://discord.test/wh", engine._discord_message("T", "b", 1)) is False

    def test_send_discord_empty_webhook_returns_false(self, engine):
        assert engine._send_discord("", engine._discord_message("T", "b", 1)) is False

    def test_send_discord_disabled_returns_false(self, engine, mocker):
        mocker.patch("config.settings.settings.DISCORD_ENABLED", False)
        assert engine._send_discord("https://x/wh", engine._discord_message("T", "b", 1)) is False

    def test_critical_routes_to_alerts_webhook_red(self, engine, mocker):
        mocker.patch("config.settings.settings.DISCORD_WEBHOOK_ALERTS", "https://discord.test/alerts")
        send = mocker.patch.object(engine, "_send_discord", return_value=True)
        assert engine.send_discord_critical("Title", "Body") is True
        url, msg = send.call_args.args
        assert url == "https://discord.test/alerts"
        assert msg["color"] == 16711680  # red

    def test_opportunity_routes_green(self, engine, mocker):
        mocker.patch("config.settings.settings.DISCORD_WEBHOOK_OPPORTUNITIES", "https://discord.test/opps")
        send = mocker.patch.object(engine, "_send_discord", return_value=True)
        engine.send_discord_opportunity("Opp", "found")
        assert send.call_args.args[1]["color"] == 65280  # green

    def test_briefing_routes_blue_no_dedup(self, engine, mocker):
        mocker.patch("config.settings.settings.DISCORD_WEBHOOK_BRIEFING", "https://discord.test/brief")
        send = mocker.patch.object(engine, "_send_discord", return_value=True)
        engine.send_discord_briefing("Brief", "today")
        engine.send_discord_briefing("Brief", "today")  # no dedup → sends twice
        assert send.call_count == 2
        assert send.call_args.args[1]["color"] == 3447003  # blue


class TestAlertEngineSignalAlert:
    def test_high_composite_routes_to_send_high(self, engine, mocker):
        high = mocker.patch.object(engine, "send_high", return_value=True)
        crit = mocker.patch.object(engine, "send_critical", return_value=True)
        state = MagicMock(current_regime="long", composite_score=70, confidence=68)
        engine.send_signal_alert(state)
        high.assert_called_once()
        crit.assert_not_called()
        assert "direction=long" in high.call_args.args[0]

    def test_critical_composite_routes_to_send_critical(self, engine, mocker):
        high = mocker.patch.object(engine, "send_high", return_value=True)
        crit = mocker.patch.object(engine, "send_critical", return_value=True)
        state = MagicMock(current_regime="strong_long", composite_score=85, confidence=90)
        engine.send_signal_alert(state)
        crit.assert_called_once()
        high.assert_not_called()


class TestAlertEngineExceptionHandling:
    def test_critical_exception_sends_sms(self, engine, mocker):
        mock_sms = mocker.patch.object(engine, "send_critical")
        exc = RiskError("daily loss limit breached")
        engine.handle_exception(exc)
        mock_sms.assert_called_once()

    def test_high_exception_sends_slack(self, engine, mocker):
        mock_slack = mocker.patch.object(engine, "send_high")
        exc = BrokerError("broker API down")
        engine.handle_exception(exc)
        mock_slack.assert_called_once()


class TestAlertEngineDbLogging:
    def test_critical_alert_logged_to_db(self, engine, mocker):
        mocker.patch.object(engine, "_send_sms", return_value=True)
        engine.send_critical("db test alert")
        with sqlite3.connect(engine._db_path) as conn:
            rows = conn.execute("SELECT channel, severity FROM alerts_log").fetchall()
        assert any(r[0] == "sms" and r[1] == "critical" for r in rows)

    def test_regime_alert_below_threshold_not_sent(self, engine, mocker):
        mock_crit = mocker.patch.object(engine, "send_critical")
        mock_high = mocker.patch.object(engine, "send_high")
        from datetime import datetime, timezone

        from signals.signal_schema import MarketState
        state = MarketState(
            current_regime="neutral",
            composite_score=50,  # below both thresholds
            last_updated=datetime.now(tz=timezone.utc),
            previous_regime="long",
        )
        engine.send_regime_alert(state)
        mock_crit.assert_not_called()
        mock_high.assert_not_called()
