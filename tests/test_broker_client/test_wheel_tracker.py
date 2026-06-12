"""Tests for broker_client/buckets/wheel_tracker.py (Step 8)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from broker_client.buckets.wheel_tracker import WheelTracker
from data.db import init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "wheel.db")
    init_db(path)
    return path


@pytest.fixture
def tracker(db):
    return WheelTracker(db_path=db)


class TestWheelCycle:
    def test_record_put_sale_creates_cycle(self, tracker):
        rid = tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        assert rid > 0
        cycles = tracker.get_cycles("HOOD")
        assert len(cycles) == 1
        assert cycles[0].current_phase == "sell_put"
        assert cycles[0].put_strike == 10.0
        assert cycles[0].cycle_number == 1

    def test_full_wheel_cycle(self, tracker):
        tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_assignment("HOOD", shares=100, cost_basis=9.5)
        tracker.record_call_sale("HOOD", strike=11.0, premium=0.4, expiry="2026-08-21")
        cycle = tracker.record_called_away("HOOD", proceeds=11.0)
        assert cycle is not None
        assert cycle.current_phase == "called_away"
        # (11 - 9.5)*100 capital + 0.4*100 call premium = 150 + 40 = 190 on 950 = 20%.
        assert cycle.cycle_return_pct == pytest.approx(20.0, abs=0.1)

    def test_phase_transitions(self, tracker):
        tracker.record_put_sale("SOFI", strike=8.0, premium=0.3, expiry="2026-07-17")
        assert tracker.get_cycles("SOFI")[-1].current_phase == "sell_put"
        tracker.record_assignment("SOFI", shares=100, cost_basis=7.7)
        assert tracker.get_cycles("SOFI")[-1].current_phase == "assigned"
        tracker.record_call_sale("SOFI", strike=9.0, premium=0.25, expiry="2026-08-21")
        assert tracker.get_cycles("SOFI")[-1].current_phase == "sell_call"
        tracker.record_called_away("SOFI", proceeds=9.0)
        assert tracker.get_cycles("SOFI")[-1].current_phase == "called_away"

    def test_premium_accumulation(self, tracker):
        tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_assignment("HOOD", shares=100, cost_basis=9.5)
        tracker.record_call_sale("HOOD", strike=11.0, premium=0.4, expiry="2026-08-21")
        cycle = tracker.get_cycles("HOOD")[-1]
        # put $50 + call $40 = $90 total premium collected.
        assert cycle.total_premium_collected == pytest.approx(90.0)

    def test_annualized_return_calculation(self, tracker, db):
        # Force known dates so days-held is deterministic.
        import sqlite3

        tracker.record_put_sale("ABC", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_assignment("ABC", shares=100, cost_basis=9.5)
        tracker.record_call_sale("ABC", strike=11.0, premium=0.4, expiry="2026-08-21")
        # Backdate the cycle start so the cycle spans ~36.5 days → 10x annualization.
        with sqlite3.connect(db) as conn:
            import json
            row = conn.execute("SELECT id, notes FROM wheel_cycles WHERE ticker='ABC'").fetchone()
            detail = json.loads(row[1])
            detail["cycle_start_date"] = "2026-01-01"
            conn.execute("UPDATE wheel_cycles SET notes=? WHERE id=?", (json.dumps(detail), row[0]))
        # Re-evaluate by recording called away (uses stored start date).
        with sqlite3.connect(db) as conn:
            import json
            row = conn.execute("SELECT id, notes FROM wheel_cycles WHERE ticker='ABC'").fetchone()
        cycle = tracker.record_called_away("ABC", proceeds=11.0)
        assert cycle.annualized_return_pct is not None
        assert cycle.annualized_return_pct > cycle.cycle_return_pct  # annualized scales up


class TestNewCyclesAndSummary:
    def test_new_cycle_after_called_away(self, tracker):
        tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_assignment("HOOD", shares=100, cost_basis=9.5)
        tracker.record_call_sale("HOOD", strike=11.0, premium=0.4, expiry="2026-08-21")
        tracker.record_called_away("HOOD", proceeds=11.0)
        # New put sale starts cycle #2.
        tracker.record_put_sale("HOOD", strike=11.0, premium=0.6, expiry="2026-09-18")
        cycles = tracker.get_cycles("HOOD")
        assert len(cycles) == 2
        assert cycles[-1].cycle_number == 2
        assert cycles[-1].current_phase == "sell_put"

    def test_get_wheel_summary(self, tracker):
        tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_assignment("HOOD", shares=100, cost_basis=9.5)
        tracker.record_call_sale("HOOD", strike=11.0, premium=0.4, expiry="2026-08-21")
        tracker.record_called_away("HOOD", proceeds=11.0)
        summary = tracker.get_wheel_summary("HOOD")
        assert summary.total_cycles == 1
        assert summary.total_premium_collected == pytest.approx(90.0)
        assert summary.avg_cycle_return_pct == pytest.approx(20.0, abs=0.1)
        assert summary.best_cycle is not None

    def test_summary_none_for_unknown_ticker(self, tracker):
        assert tracker.get_wheel_summary("NOPE") is None

    def test_get_all_wheels(self, tracker):
        tracker.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        tracker.record_put_sale("SOFI", strike=8.0, premium=0.3, expiry="2026-07-17")
        wheels = tracker.get_all_wheels()
        assert {w.ticker for w in wheels} == {"HOOD", "SOFI"}


class TestEdgeCases:
    def test_assignment_without_open_cycle_noop(self, tracker):
        tracker.record_assignment("ZZZ", shares=100, cost_basis=5.0)  # no raise
        assert tracker.get_cycles("ZZZ") == []

    def test_called_away_without_cycle_returns_none(self, tracker):
        assert tracker.record_called_away("ZZZ", proceeds=5.0) is None

    def test_assignment_alert_fires(self, db):
        alerts = MagicMock()
        t = WheelTracker(db_path=db, alert_engine=alerts)
        t.record_put_sale("HOOD", strike=10.0, premium=0.5, expiry="2026-07-17")
        t.record_assignment("HOOD", shares=100, cost_basis=9.5)
        assert any("ASSIGNED" in c.kwargs.get("title", "")
                   for c in alerts.send_alert.call_args_list)
