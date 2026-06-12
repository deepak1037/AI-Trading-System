"""Tests for the Phase 2b real-time presidential monitor.

Covers source detection + priority/fallback, the longest-match-wins classifier
(including the live miss "cancelled strikes" → LONG), dedup, freshness gating,
instrument suggestions, alert firing, graceful failure, and WebSocket reconnect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from config.settings import settings
from signals.presidential_monitor import (
    PresidentialSignal,
    RealTimePresidentialMonitor,
)


@pytest.fixture
def monitor() -> RealTimePresidentialMonitor:
    return RealTimePresidentialMonitor(alert_engine=MagicMock())


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ── Source detection ──────────────────────────────────────────────────────────
def test_source_detection_scrapecreators(monitor, monkeypatch) -> None:
    """Uses ScrapeCreators when its key is set and it responds."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "sc-key")
    monkeypatch.setattr(monitor, "_fetch_scrapecreators", lambda: [])
    assert monitor.detect_active_source() == "scrapecreators"


def test_source_detection_tweetstream(monitor, monkeypatch) -> None:
    """Uses TweetStream when SC is unavailable but a TweetStream key is set."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "ts-key")
    assert monitor.detect_active_source() == "tweetstream"


def test_source_detection_rss_fallback(monitor, monkeypatch) -> None:
    """Falls back to RSS when no API keys are configured."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "")
    assert monitor.detect_active_source() == "rss"


def test_source_priority_prefers_scrapecreators(monitor, monkeypatch) -> None:
    """With multiple keys set, the first in priority order wins."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "sc-key")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "ts-key")
    monkeypatch.setattr(monitor, "_fetch_scrapecreators", lambda: [])
    assert monitor.detect_active_source() == "scrapecreators"


# ── Classification ────────────────────────────────────────────────────────────
def test_classify_cancelled_strikes_long(monitor) -> None:
    """'I cancelled the Iran strikes' → LONG (the live miss)."""
    signal = monitor._classify_post(
        "I cancelled the scheduled strikes against Iran"
    )
    assert signal is not None
    assert signal.direction == "long"


def test_classify_new_tariffs_short(monitor) -> None:
    """'New 50% tariffs on China' → SHORT."""
    signal = monitor._classify_post(
        "I am placing NEW 50% TARIFFS on China immediately"
    )
    assert signal is not None
    assert signal.direction == "short"


def test_classify_tariff_reduction_long(monitor) -> None:
    """'tariff reduction' (long phrase) beats 'tariff' → LONG."""
    signal = monitor._classify_post("Announcing a major tariff reduction with China")
    assert signal is not None
    assert signal.direction == "long"


def test_classify_no_deal_short(monitor) -> None:
    """'NO DEAL. Terminated.' → SHORT ('no deal' beats 'deal')."""
    signal = monitor._classify_post("NO DEAL. The negotiations are Terminated.")
    assert signal is not None
    assert signal.direction == "short"


def test_classify_no_market_keywords(monitor) -> None:
    """Non-market posts return None (no noise)."""
    assert monitor._classify_post("Congratulations to the great state of Texas!") is None


def test_classify_confidence_set(monitor) -> None:
    """Classified signals carry the configured shock confidence."""
    signal = monitor._classify_post("New tariffs on everything")
    assert signal is not None
    assert signal.confidence == settings.PRESIDENTIAL_SHOCK_CONFIDENCE


# ── Instrument suggestions ────────────────────────────────────────────────────
def test_instrument_suggestions_iran(monitor) -> None:
    """Iran de-escalation → airline calls + oil puts."""
    signal = monitor._classify_post("I cancelled strikes on Iran — peace at hand")
    assert signal is not None
    assert any("JETS" in i for i in signal.suggested_instruments)


def test_instrument_suggestions_tariff(monitor) -> None:
    """Tariff reduction → QQQ/retail calls."""
    signal = monitor._classify_post("A major tariff reduction is coming")
    assert signal is not None
    assert any("XRT" in i or "QQQ" in i for i in signal.suggested_instruments)


def test_instrument_suggestions_short_tariff(monitor) -> None:
    """New tariffs → puts (China FXI puts)."""
    instruments = monitor._get_instruments("short", ["tariff"])
    assert any("puts" in i for i in instruments)


# ── Dedup + freshness ─────────────────────────────────────────────────────────
def test_dedup_same_post(monitor, monkeypatch) -> None:
    """Same post_id is processed (and alerted) only once."""
    monkeypatch.setattr(monitor, "_log_to_db", lambda *a, **k: None)
    for _ in range(2):
        monitor._process_post(
            post_id="p1",
            text="New tariffs on China",
            created_at=_now_iso(),
            url="http://x",
            source="rss",
        )
    assert monitor.alert_engine.send_alert.call_count == 1


def test_only_recent_posts(monitor, monkeypatch) -> None:
    """Posts older than PRESIDENTIAL_FRESH_MINUTES are ignored."""
    monkeypatch.setattr(monitor, "_log_to_db", lambda *a, **k: None)
    old = (
        datetime.now(tz=UTC)
        - timedelta(minutes=settings.PRESIDENTIAL_FRESH_MINUTES + 5)
    ).isoformat()
    monitor._process_post(
        post_id="old1",
        text="New tariffs on China",
        created_at=old,
        url="http://x",
        source="rss",
    )
    monitor.alert_engine.send_alert.assert_not_called()


def test_unparseable_timestamp_is_recent(monitor) -> None:
    """An odd/empty timestamp is treated as recent, never silently dropped."""
    assert monitor._is_recent("") is True
    assert monitor._is_recent("not-a-date") is True


def test_rss_rfc2822_timestamp_parsed(monitor) -> None:
    """RSS RFC-2822 pubDate strings parse correctly."""
    recent = datetime.now(tz=UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    assert monitor._is_recent(recent) is True


# ── Alerting ──────────────────────────────────────────────────────────────────
def test_alert_fires_on_market_post(monitor, monkeypatch) -> None:
    """A market-relevant fresh post fires a Discord alert via AlertEngine."""
    monkeypatch.setattr(monitor, "_log_to_db", lambda *a, **k: None)
    monitor._process_post(
        post_id="m1",
        text="Massive new tariffs on China starting Monday",
        created_at=_now_iso(),
        url="http://x",
        source="scrapecreators",
    )
    monitor.alert_engine.send_alert.assert_called_once()
    kwargs = monitor.alert_engine.send_alert.call_args.kwargs
    assert kwargs["channel"] == "alerts"
    assert kwargs["color"] == "red"


def test_no_alert_on_nonmarket_post(monitor, monkeypatch) -> None:
    """A non-market post fires no alert."""
    monkeypatch.setattr(monitor, "_log_to_db", lambda *a, **k: None)
    monitor._process_post(
        post_id="n1",
        text="Thank you to my wonderful supporters in Ohio!",
        created_at=_now_iso(),
        url="http://x",
        source="rss",
    )
    monitor.alert_engine.send_alert.assert_not_called()


def test_long_post_alert_color_green(monitor, monkeypatch) -> None:
    """A bullish post routes a green embed."""
    monkeypatch.setattr(monitor, "_log_to_db", lambda *a, **k: None)
    monitor._process_post(
        post_id="g1",
        text="We just signed a historic trade deal — tariff reduction for all",
        created_at=_now_iso(),
        url="http://x",
        source="tweetstream",
    )
    kwargs = monitor.alert_engine.send_alert.call_args.kwargs
    assert kwargs["color"] == "green"


# ── Graceful degradation + lifecycle ──────────────────────────────────────────
def test_graceful_failure(monitor, monkeypatch) -> None:
    """All sources failing → falls back to RSS, never raises."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "sc")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "ts")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "ap")

    def _boom() -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(monitor, "_fetch_scrapecreators", _boom)
    monkeypatch.setattr(monitor, "_test_tweetstream_auth", _boom)
    monkeypatch.setattr(monitor, "_fetch_apify", _boom)
    assert monitor.detect_active_source() == "rss"


def test_process_post_never_raises(monitor, monkeypatch) -> None:
    """A failure deep in processing is swallowed (loop must survive)."""
    def _boom(*a, **k) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(monitor, "_log_to_db", _boom)
    # Should not raise even though _log_to_db blows up.
    monitor._process_post(
        post_id="e1",
        text="New tariffs",
        created_at=_now_iso(),
        url="http://x",
        source="rss",
    )


def test_websocket_reconnects(monitor, monkeypatch) -> None:
    """_run_tweetstream calls run_forever with auto-reconnect."""
    import websocket

    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "tok")
    calls: dict[str, object] = {"n": 0, "reconnect": None}
    fake_ws = MagicMock()

    def _run_forever(**kwargs: object) -> None:
        calls["n"] = int(calls["n"]) + 1  # type: ignore[call-overload]
        calls["reconnect"] = kwargs.get("reconnect")
        monitor._running = False  # break the loop after one pass

    fake_ws.run_forever.side_effect = _run_forever
    monkeypatch.setattr(websocket, "WebSocketApp", MagicMock(return_value=fake_ws))

    monitor._running = True
    monitor._run_tweetstream()
    assert calls["n"] == 1
    assert calls["reconnect"] == 5


def test_start_sets_active_source(monkeypatch) -> None:
    """start() detects the source and spawns a daemon thread."""
    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "")
    # Stop the RSS loop immediately so the test doesn't hang.
    monkeypatch.setattr(mon, "_run_rss_loop", lambda: None)
    mon.start()
    assert mon.active_source == "rss"
    assert mon._thread is not None
    mon.stop()


def test_db_logging_roundtrip(monkeypatch, tmp_path) -> None:
    """_log_to_db persists a row into presidential_signals."""
    import sqlite3

    from data.db import init_db

    db = tmp_path / "t.db"
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    init_db(str(db))

    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    sig = PresidentialSignal(
        direction="short", confidence=80, keywords=["tariff"],
        suggested_instruments=["QQQ puts"],
    )
    mon._log_to_db("post-99", "New tariffs", "http://x", sig, "rss")

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT direction, source FROM presidential_signals WHERE post_id=?",
            ("post-99",),
        ).fetchone()
    assert row == ("short", "rss")
