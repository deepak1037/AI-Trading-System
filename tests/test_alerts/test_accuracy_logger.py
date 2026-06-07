"""Tests for alerts/accuracy_logger.py."""

from __future__ import annotations

import sqlite3

import pytest

from alerts.accuracy_logger import AccuracyLogger


@pytest.fixture
def logger_with_db(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE accuracy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                predicted_direction TEXT NOT NULL,
                actual_direction TEXT,
                actual_pct_move REAL,
                was_correct INTEGER,
                date TEXT NOT NULL
            );
        """)
    return AccuracyLogger(db_path=str(db))


class TestAccuracyLogger:
    def test_log_prediction(self, logger_with_db):
        row_id = logger_with_db.log_prediction(
            signal_id=1,
            predicted_direction="long",
            log_date="2024-01-15",
        )
        assert row_id > 0

    def test_directions_agree_bullish(self):
        assert AccuracyLogger._directions_agree("long", "strong_long") is True
        assert AccuracyLogger._directions_agree("strong_long", "long") is True

    def test_directions_agree_bearish(self):
        assert AccuracyLogger._directions_agree("short", "strong_short") is True

    def test_directions_disagree(self):
        assert AccuracyLogger._directions_agree("long", "short") is False
        assert AccuracyLogger._directions_agree("long", "neutral") is False

    def test_fill_actual_updates_rows(self, logger_with_db, mocker):
        # Insert a prediction
        logger_with_db.log_prediction(1, "long", "2024-01-15")
        # Mock actual direction
        mocker.patch.object(
            logger_with_db,
            "_get_actual_direction",
            return_value=("long", 0.012),
        )
        updated = logger_with_db.fill_actual(log_date="2024-01-15")
        assert updated == 1
        # Verify was_correct=1
        with sqlite3.connect(logger_with_db._db_path) as conn:
            row = conn.execute("SELECT was_correct FROM accuracy_log").fetchone()
        assert row[0] == 1

    def test_fill_actual_marks_wrong(self, logger_with_db, mocker):
        logger_with_db.log_prediction(1, "long", "2024-01-15")
        mocker.patch.object(
            logger_with_db,
            "_get_actual_direction",
            return_value=("short", -0.015),
        )
        logger_with_db.fill_actual(log_date="2024-01-15")
        with sqlite3.connect(logger_with_db._db_path) as conn:
            row = conn.execute("SELECT was_correct FROM accuracy_log").fetchone()
        assert row[0] == 0

    def test_get_accuracy_empty(self, logger_with_db):
        result = logger_with_db.get_accuracy(days=30)
        assert result["total"] == 0
        assert result["accuracy_pct"] == 0.0

    def test_get_accuracy_with_data(self, logger_with_db, mocker):
        from datetime import date
        today = date.today().isoformat()
        mocker.patch.object(
            logger_with_db,
            "_get_actual_direction",
            return_value=("long", 0.01),
        )
        for i in range(5):
            logger_with_db.log_prediction(i, "long", today)
        logger_with_db.fill_actual(today)
        result = logger_with_db.get_accuracy(days=30)
        assert result["total"] == 5
        assert result["correct"] == 5
        assert result["accuracy_pct"] == 100.0
