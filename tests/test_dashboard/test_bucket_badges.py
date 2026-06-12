"""Tests for dashboard/bucket_badges.py + new dashboard pages (Step 10)."""

from __future__ import annotations

from pathlib import Path

from dashboard.bucket_badges import (
    bucket_badge,
    dte_progress_bar,
    exit_recommendation,
    recoverable_badge,
    row_to_bucket_position,
    thesis_label,
)


class TestBadges:
    def test_bucket_badge_b1(self):
        assert "B1-MSP" in bucket_badge(1)

    def test_bucket_badge_b2(self):
        assert "B2-Earnings" in bucket_badge(2)

    def test_bucket_badge_b3(self):
        assert "B3-LEAP" in bucket_badge(3)

    def test_bucket_badge_with_subtype(self):
        assert "(leap)" in bucket_badge(3, "leap")

    def test_bucket_badge_unknown(self):
        assert "B?" in bucket_badge(None)

    def test_recoverable_true(self):
        assert "Recoverable" in recoverable_badge(True)

    def test_recoverable_false(self):
        assert "Defined Risk" in recoverable_badge(False)

    def test_recoverable_int(self):
        # SQLite stores booleans as 0/1.
        assert "Defined Risk" in recoverable_badge(0)
        assert "Recoverable" in recoverable_badge(1)


class TestDteProgress:
    def test_full_remaining(self):
        bar = dte_progress_bar(40, 40, width=10)
        assert bar.startswith("░" * 10)  # 0% elapsed
        assert "40d" in bar

    def test_half_used(self):
        bar = dte_progress_bar(20, 40, width=10)
        assert bar.startswith("█████░░░░░")  # 50% elapsed
        assert "20d" in bar

    def test_no_original(self):
        bar = dte_progress_bar(15, 0)
        assert "15d" in bar


class TestThesisLabel:
    def test_thesis_label(self):
        assert thesis_label("active", 40.0) == "active (40% complete)"

    def test_thesis_label_defaults(self):
        assert "active" in thesis_label(None, None)


class TestExitRecommendation:
    def test_row_to_bucket_position(self):
        bp = row_to_bucket_position({"ticker": "NVDA", "bucket": 3, "sub_type": "leap"})
        assert bp.ticker == "NVDA"
        assert bp.bucket == 3

    def test_exit_recommendation_from_row(self):
        # Bucket 1 short DTE at 55% profit → FULL_EXIT.
        rec = exit_recommendation({
            "ticker": "HOOD", "bucket": 1, "sub_type": "msp",
            "dte_remaining": 10, "profit_pct": 55,
        })
        assert rec == "FULL_EXIT"

    def test_exit_recommendation_hold(self):
        rec = exit_recommendation({
            "ticker": "HOOD", "bucket": 1, "sub_type": "msp",
            "dte_remaining": 60, "profit_pct": 5, "original_dte": 90,
        })
        assert rec == "HOLD"


class TestDashboardPages:
    def test_wheel_tracker_page_exists(self):
        assert Path("dashboard/pages/7_wheel_tracker.py").exists()

    def test_bucket_badges_module_exists(self):
        assert Path("dashboard/bucket_badges.py").exists()

    def test_pages_parse_cleanly(self):
        import ast

        for fname in ("1_overview.py", "2_positions.py", "7_wheel_tracker.py"):
            path = Path("dashboard/pages") / fname
            ast.parse(path.read_text(), filename=str(path))
