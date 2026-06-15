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
    _MAX_CONSECUTIVE_FAILURES,
    PresidentialSignal,
    RealTimePresidentialMonitor,
    _SourceExhausted,
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
# ── ScrapeCreators 402 / runtime fallback (bug fix) ─────────────────────────────
def test_detect_skips_empty_scrapecreators_key(monitor, monkeypatch) -> None:
    """An empty/whitespace SC key → never probe ScrapeCreators, go straight to RSS."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "   ")  # whitespace only
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "")
    called = []
    monkeypatch.setattr(monitor, "_fetch_scrapecreators", lambda: called.append(1) or [])
    assert monitor.detect_active_source() == "rss"
    assert called == []  # SC fetch was never attempted


def test_fetch_scrapecreators_402_raises_exhausted(monitor, monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "sc")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: MagicMock(status_code=402))
    with pytest.raises(_SourceExhausted):
        monitor._fetch_scrapecreators()


def test_scrapecreators_loop_returns_on_402(monitor, monkeypatch) -> None:
    """The loop must RETURN on 402 (not retry forever)."""
    monitor._running = True

    def _402():
        raise _SourceExhausted("ScrapeCreators quota exhausted (402)")

    monkeypatch.setattr(monitor, "_fetch_scrapecreators", _402)
    monitor._run_scrapecreators_loop()  # returns immediately — no hang
    assert monitor._running  # didn't stop the monitor, just gave up on this source


def test_poll_loop_gives_up_after_max_failures(monitor, monkeypatch) -> None:
    monitor._running = True
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("network down")

    monitor._poll_loop("X", "scrapecreators", _boom, interval=0.0)
    assert calls["n"] == _MAX_CONSECUTIVE_FAILURES  # switched after N consecutive fails


def test_run_chain_switches_scrapecreators_to_rss(monitor, monkeypatch) -> None:
    """SC loop gives up (402) → chain advances to RSS automatically."""
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "sc")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "")
    monitor._running = True
    order: list[str] = []

    monkeypatch.setattr(monitor, "_run_scrapecreators_loop", lambda: order.append("sc"))

    def _rss():
        order.append("rss")
        monitor._running = False  # terminal — stop after RSS starts

    monkeypatch.setattr(monitor, "_run_rss_loop", _rss)
    monitor._run_chain("scrapecreators")
    assert order == ["sc", "rss"]          # SC gave up → switched to RSS
    assert monitor.active_source == "rss"


def test_usable_sources_excludes_keyless(monitor, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SCRAPECREATORS_API_KEY", "")
    monkeypatch.setattr(settings, "TWEETSTREAM_API_KEY", "ts")
    monkeypatch.setattr(settings, "APIFY_API_KEY", "")
    usable = monitor._usable_sources()
    assert "scrapecreators" not in usable
    assert "tweetstream" in usable
    assert usable[-1] == "rss"  # RSS always terminal


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
    # Stop the RSS loop immediately so the test doesn't hang; skip the live
    # catch-up fetch so the daemon thread makes no network call.
    monkeypatch.setattr(mon, "_run_rss_loop", lambda: None)
    monkeypatch.setattr(mon, "_startup_catchup", lambda _source: None)
    mon.start()
    assert mon.active_source == "rss"
    assert mon._thread is not None
    mon.stop()


def test_get_last_processed_timestamp(monkeypatch, tmp_path) -> None:
    """Returns the parsed MAX(created_at) from presidential_signals."""
    from data.db import get_connection, init_db

    db = tmp_path / "t.db"
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    init_db(str(db))
    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    assert mon._get_last_processed_timestamp() is None  # empty table

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO presidential_signals "
            "(post_id, post_text, source, direction, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("p1", "deal", "rss", "long", 70, "2026-06-14T20:00:00+00:00"),
        )
    ts = mon._get_last_processed_timestamp()
    assert ts is not None
    assert ts.isoformat() == "2026-06-14T20:00:00+00:00"


def test_startup_catchup_replays_missed_posts(monkeypatch) -> None:
    """Posts published after last_seen are processed; older ones are skipped."""
    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    monkeypatch.setattr(
        mon, "_get_last_processed_timestamp",
        lambda: datetime(2026, 6, 14, 20, 0, tzinfo=UTC),
    )
    posts = [
        {"post_id": "before", "text": "Deal signed last week",
         "created_at": "2026-06-14T19:50:00+00:00", "url": "u0"},
        {"post_id": "iran1", "text": "Deal with Iran is complete — peace!",
         "created_at": "2026-06-14T21:29:00+00:00", "url": "u1"},
        {"post_id": "iran2", "text": "Great Deal for Peace, signing soon",
         "created_at": "2026-06-14T22:27:00+00:00", "url": "u2"},
    ]
    monkeypatch.setattr(mon, "_fetch_posts", lambda _source: posts)
    logged: list = []
    monkeypatch.setattr(
        mon, "_log_to_db",
        lambda post_id, text, url, signal, source: logged.append((post_id, signal)),
    )
    monkeypatch.setattr(
        mon, "_send_alert", lambda text, url, signal, source: None
    )

    mon._startup_catchup("rss")

    # Only the two post-cutoff Iran-deal posts were processed, both LONG.
    assert [pid for pid, _ in logged] == ["iran1", "iran2"]
    assert all(sig.direction == "long" for _, sig in logged)


def test_startup_catchup_first_run_uses_window(monkeypatch) -> None:
    """With no prior signal, cutoff falls back to the catch-up window."""
    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    monkeypatch.setattr(mon, "_get_last_processed_timestamp", lambda: None)
    recent = (
        datetime.now(tz=UTC) - timedelta(minutes=5)
    ).isoformat()
    old = (
        datetime.now(tz=UTC)
        - timedelta(minutes=settings.PRESIDENTIAL_CATCHUP_MINUTES + 30)
    ).isoformat()
    posts = [
        {"post_id": "old", "text": "Deal done", "created_at": old, "url": "u0"},
        {"post_id": "fresh", "text": "New trade deal and peace",
         "created_at": recent, "url": "u1"},
    ]
    monkeypatch.setattr(mon, "_fetch_posts", lambda _source: posts)
    logged: list = []
    monkeypatch.setattr(
        mon, "_log_to_db",
        lambda post_id, text, url, signal, source: logged.append(post_id),
    )
    monkeypatch.setattr(
        mon, "_send_alert", lambda text, url, signal, source: None
    )

    mon._startup_catchup("rss")

    assert logged == ["fresh"]


def test_classify_count_long_beats_short(monkeypatch) -> None:
    """More LONG keyword hits than SHORT → long, regardless of order."""
    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    sig = mon._classify_post(
        "We reached a deal and peace, signing soon despite the blockade"
    )
    # deal/peace/signing (3 LONG) outvote blockade (1 SHORT).
    assert sig is not None
    assert sig.direction == "long"


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


# ── Context filter (false-positive suppression) ───────────────────────────────
def test_context_filter_opponent_rant_skipped(monitor) -> None:
    """A border rant naming Biden matches 'invasion' but must NOT alert."""
    assert monitor._classify_post(
        "Biden poured millions into the invasion of our southern border — a disaster"
    ) is None


def test_context_filter_historical_no_opponent_skipped(monitor) -> None:
    """Past-tense single-keyword commentary (no opponent name) is skipped."""
    assert monitor._classify_post(
        "The invasion was the worst thing that ever happened, years ago"
    ) is None


def test_context_filter_dumocrat_post_skipped(monitor) -> None:
    """Opponent slur ('Dumocrats') cancels even with a market keyword."""
    assert monitor._classify_post(
        "The Dumocrats want more tariffs — terrible for the country"
    ) is None


def test_context_filter_lone_keyword_no_anchor_skipped(monitor) -> None:
    """A single market keyword with no current-action anchor is suppressed."""
    assert monitor._classify_post("It was an invasion of privacy, frankly") is None


def test_context_filter_iran_deal_still_long(monitor) -> None:
    """Verify scenario: Iran deal post → LONG alert."""
    sig = monitor._classify_post(
        "We are signing a historic peace deal with Iran today"
    )
    assert sig is not None
    assert sig.direction == "long"


def test_context_filter_new_tariff_still_short(monitor) -> None:
    """Verify scenario: new tariff post → SHORT alert."""
    sig = monitor._classify_post("Imposing new tariffs on China effective today")
    assert sig is not None
    assert sig.direction == "short"


def test_db_dedup_blocks_realert(monkeypatch, tmp_path) -> None:
    """A post_id already in presidential_signals is never alerted again."""
    from data.db import get_connection, init_db

    db = tmp_path / "t.db"
    monkeypatch.setattr(settings, "DB_PATH", str(db))
    init_db(str(db))
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO presidential_signals "
            "(post_id, post_text, source, direction, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dup1", "New tariffs on China", "rss", "short", 80,
             "2026-06-15T03:40:00+00:00"),
        )

    mon = RealTimePresidentialMonitor(alert_engine=MagicMock())
    mon._process_post(
        post_id="dup1",
        text="New tariffs on China",
        created_at=_now_iso(),
        url="http://x",
        source="rss",
    )
    mon.alert_engine.send_alert.assert_not_called()
