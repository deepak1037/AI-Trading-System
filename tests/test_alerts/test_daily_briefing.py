"""Tests for alerts/daily_briefing.py (Step 9)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alerts.daily_briefing import DailyBriefing
from broker_client.buckets.bucket_manager import BucketManager
from data.db import init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "brief.db")
    init_db(path)
    return path


@pytest.fixture
def briefing(db):
    return DailyBriefing(bucket_manager=BucketManager(db_path=db))


class TestGenerate:
    def test_briefing_generates_without_error(self, briefing):
        text = briefing.generate_briefing()
        assert isinstance(text, str)
        assert len(text) > 0

    def test_briefing_contains_required_sections(self, briefing):
        text = briefing.generate_briefing()
        for section in (
            "GOOD MORNING",
            "MACRO REGIME",
            "OPEN POSITIONS",
            "TODAY'S OPPORTUNITIES",
            "PORTFOLIO HEALTH",
            "BUCKET PERFORMANCE",
        ):
            assert section in text

    def test_macro_regime_rendered(self, db):
        state = SimpleNamespace(current_regime="long", composite_score=72)
        b = DailyBriefing(bucket_manager=BucketManager(db_path=db), market_state=state)
        text = b.generate_briefing()
        assert "LONG" in text
        assert "72/100" in text

    def test_dte_positions_flagged(self, db):
        watcher = SimpleNamespace(positions={
            "HOOD": {"ticker": "HOOD", "dte_remaining": 18},
            "AAPL": {"ticker": "AAPL", "dte_remaining": 60},
        })
        b = DailyBriefing(bucket_manager=BucketManager(db_path=db), position_watcher=watcher)
        text = b.generate_briefing()
        assert "HOOD" in text  # inside 21-DTE window
        assert "2 total" in text

    def test_paper_account_equity_rendered(self, db):
        perf = SimpleNamespace(current_equity=59680.5, total_return_pct=19.36)
        paper = SimpleNamespace(get_performance=lambda: perf)
        b = DailyBriefing(bucket_manager=BucketManager(db_path=db), paper_account=paper)
        text = b.generate_briefing()
        assert "59,680" in text
        assert "+19.36%" in text

    def test_events_provider(self, db):
        events = SimpleNamespace(
            todays_events=lambda: ["CPI 8:30 AM", "Fed minutes 2 PM"],
            opportunities=lambda: ["NVDA earnings put"],
        )
        b = DailyBriefing(bucket_manager=BucketManager(db_path=db), events_provider=events)
        text = b.generate_briefing()
        assert "CPI 8:30 AM" in text
        assert "NVDA earnings put" in text

    def test_handles_broken_data_source(self, db):
        bad = SimpleNamespace(get_performance=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        b = DailyBriefing(bucket_manager=BucketManager(db_path=db), paper_account=bad)
        # Must not raise — degrades to n/a.
        text = b.generate_briefing()
        assert "PORTFOLIO HEALTH" in text


class TestSend:
    def test_send_briefing_to_discord(self, db):
        alerts = MagicMock()
        alerts.send_alert.return_value = True
        b = DailyBriefing(alert_engine=alerts, bucket_manager=BucketManager(db_path=db))
        assert b.send_briefing() is True
        kwargs = alerts.send_alert.call_args.kwargs
        assert kwargs["channel"] == "briefing"
        assert kwargs["color"] == "blue"

    def test_send_without_engine_returns_false(self, briefing):
        assert briefing.send_briefing() is False

    def test_send_swallows_alert_errors(self, db):
        alerts = MagicMock()
        alerts.send_alert.side_effect = RuntimeError("discord down")
        b = DailyBriefing(alert_engine=alerts, bucket_manager=BucketManager(db_path=db))
        assert b.send_briefing() is False
