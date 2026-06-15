"""Tests for broker_client/analytics/signal_accuracy.py."""

from __future__ import annotations

from datetime import date

import pytest

from broker_client.analytics.signal_accuracy import SignalAccuracyTracker
from data.db import get_connection, init_db


@pytest.fixture
def tracker(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return SignalAccuracyTracker(db_path=db_path)


def _log(tracker: SignalAccuracyTracker, direction: str, confidence: int,
         sources: list, correct: bool, score: int = 60) -> None:
    tracker.track_signal(
        signal_date=date.today().isoformat(),
        direction=direction,
        confidence=confidence,
        composite_score=score,
        sources=sources,
        trade_id=None,
        was_correct=correct,
    )


def test_long_signal_correct_when_profitable(tracker):
    _log(tracker, "long", 70, ["macro", "technical"], True)
    with get_connection(tracker._db_path) as conn:
        row = conn.execute("SELECT was_correct FROM signal_accuracy").fetchone()
    assert row["was_correct"] == 1


def test_neutral_signal_correct_when_flat(tracker):
    _log(tracker, "neutral", 55, ["macro"], True)
    with get_connection(tracker._db_path) as conn:
        row = conn.execute("SELECT was_correct, direction FROM signal_accuracy").fetchone()
    assert row["was_correct"] == 1
    assert row["direction"] == "neutral"


def test_accuracy_by_source(tracker, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_SIGNALS_FOR_ACCURACY", 2)

    _log(tracker, "long", 70, ["macro"], True)
    _log(tracker, "long", 65, ["macro"], True)
    _log(tracker, "short", 60, ["sentiment"], False)

    report = tracker.generate_report("MTD")
    assert not report.insufficient_data
    assert "macro" in report.source_accuracy
    assert report.source_accuracy["macro"] == pytest.approx(1.0)


def test_optimal_threshold_calculation(tracker, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_SIGNALS_FOR_ACCURACY", 3)

    # High-confidence signals are all correct, low-confidence are wrong.
    # At threshold ≥55 only the two high-confidence signals qualify → 100% accuracy
    # (the low-confidence wrong signal at 45 is excluded above 55).
    _log(tracker, "long", 85, ["fusion"], True, score=75)
    _log(tracker, "long", 82, ["fusion"], True, score=73)
    _log(tracker, "short", 45, ["sentiment"], False, score=35)

    report = tracker.generate_report("MTD")
    assert not report.insufficient_data
    # Should find a threshold where accuracy is maximised (≥55 excludes the wrong signal)
    assert report.optimal_confidence_threshold >= 50


def test_insufficient_data_before_min_signals(tracker, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_SIGNALS_FOR_ACCURACY", 10)
    _log(tracker, "long", 70, ["macro"], True)
    report = tracker.generate_report("MTD")
    assert report.insufficient_data is True


def test_high_confidence_accuracy(tracker, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "MIN_SIGNALS_FOR_ACCURACY", 2)

    _log(tracker, "long", 80, ["macro"], True)
    _log(tracker, "short", 30, ["sentiment"], False)  # low-confidence, wrong

    report = tracker.generate_report("MTD")
    # Only the high-confidence signal feeds into high_confidence_accuracy
    assert report.high_confidence_accuracy == pytest.approx(1.0)
