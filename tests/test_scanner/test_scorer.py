"""Tests for scanner/scorer.py (Days 13-18)."""

from __future__ import annotations

from scanner.scorer import CompositeScorer


class TestCompositeScorer:
    def setup_method(self):
        self.scorer = CompositeScorer()

    def test_score_all_zeros(self):
        score = self.scorer.score(0, 0, 0, 0, 0)
        assert score == 0

    def test_score_all_hundreds(self):
        score = self.scorer.score(100, 100, 100, 100, 100)
        assert score == 100

    def test_score_partial(self):
        # Only fundamental at 100 → fundamental weight (35) % of 100 = 35
        score = self.scorer.score(fundamental_score=100.0)
        assert score == self.scorer.WEIGHTS["fundamental"]

    def test_score_weights_sum_to_100(self):
        total = sum(self.scorer.WEIGHTS.values())
        assert total == 100

    def test_score_clamped_to_100(self):
        score = self.scorer.score(200, 200, 200, 200, 200)
        assert score == 100

    def test_score_clamped_to_zero(self):
        score = self.scorer.score(-100, -100, -100, -100, -100)
        assert score == 0

    def test_fundamental_score_all_flags(self):
        s = self.scorer.fundamental_score_from_flags(True, True, True)
        assert s == 100.0

    def test_fundamental_score_no_flags(self):
        s = self.scorer.fundamental_score_from_flags(False, False, False)
        assert s == 0.0

    def test_fundamental_score_eps_only(self):
        s = self.scorer.fundamental_score_from_flags(True, False, False)
        assert s == 50.0

    def test_institutional_score_high_holders(self):
        s = self.scorer.institutional_score_from_data(50, 0.5, 4)
        assert s <= 100.0
        assert s > 50.0

    def test_technical_score_stage2_base_rs(self):
        s = self.scorer.technical_score_from_flags(True, True, 90.0)
        assert s > 70.0

    def test_technical_score_nothing(self):
        s = self.scorer.technical_score_from_flags(False, False, 0.0)
        assert s == 0.0

    def test_squeeze_score_high_short(self):
        s = self.scorer.squeeze_score_from_short(10.0, 15.0)
        assert s > 50.0

    def test_squeeze_score_zero(self):
        s = self.scorer.squeeze_score_from_short(0.0, 0.0)
        assert s == 0.0

    def test_passes_threshold_above(self):
        assert self.scorer.passes_watchlist_threshold(70) is True

    def test_passes_threshold_below(self):
        assert self.scorer.passes_watchlist_threshold(50) is False

    def test_passes_threshold_at_boundary(self):
        # Default threshold is 65
        assert self.scorer.passes_watchlist_threshold(65) is True
        assert self.scorer.passes_watchlist_threshold(64) is False
