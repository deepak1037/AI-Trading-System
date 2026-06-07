"""Tests for signals/sentiment_scorer.py — uses MockSentimentScorer (no model)."""

from __future__ import annotations

import pytest

from signals.sentiment_scorer import MockSentimentScorer, SentimentScorer


@pytest.fixture
def scorer():
    return MockSentimentScorer()


class TestSentimentScorerBasic:
    def test_empty_headlines_returns_neutral(self, scorer):
        signal = scorer.score_headlines([])
        assert signal.direction == "neutral"
        assert signal.source == "sentiment"
        assert signal.confidence == 0

    def test_bullish_headlines(self, scorer):
        headlines = [
            "Markets surge on strong earnings beat",
            "Stocks rally to record highs",
            "Tech sector gains on strong jobs report",
        ]
        signal = scorer.score_headlines(headlines)
        assert signal.direction in ("long", "strong_long", "neutral")
        assert signal.source == "sentiment"

    def test_bearish_headlines(self, scorer):
        headlines = [
            "Markets plunge on weak economic data",
            "Stocks fall sharply as recession fears grow",
            "S&P 500 drops amid rate cut concerns",
        ]
        signal = scorer.score_headlines(headlines)
        assert signal.direction in ("short", "strong_short", "neutral")

    def test_mixed_headlines_near_neutral(self, scorer):
        headlines = [
            "Markets surge on strong earnings",
            "Oil prices drop on demand fears",
        ]
        signal = scorer.score_headlines(headlines)
        assert signal.source == "sentiment"
        assert 0 <= signal.confidence <= 100

    def test_metadata_fields(self, scorer):
        headlines = ["Stocks gain as inflation falls", "Markets rally strongly"]
        signal = scorer.score_headlines(headlines)
        assert "headline_count" in signal.metadata
        assert "avg_score" in signal.metadata
        assert "bullish" in signal.metadata
        assert "bearish" in signal.metadata

    def test_confidence_in_range(self, scorer):
        for headlines in [
            ["Stocks surge strongly upward"],
            ["Markets plunge sharply down"],
            ["Economy shows mixed results"],
        ]:
            signal = scorer.score_headlines(headlines)
            assert 0 <= signal.confidence <= 100


class TestSentimentScorerNewsAPI:
    def test_newsapi_unavailable_falls_back_to_neutral(self, scorer, mocker):
        """When NewsAPI key absent, score() should return a valid signal."""
        mocker.patch.object(scorer, "_fetch_newsapi_headlines", return_value=[])
        mocker.patch.object(scorer, "_fetch_rss_headlines", return_value=[])
        signal = scorer.score()
        assert signal.source == "sentiment"
        assert signal.direction == "neutral"

    def test_newsapi_error_is_caught(self, scorer, mocker):
        from core.exceptions import DataError
        mocker.patch.object(
            scorer, "_fetch_newsapi_headlines", side_effect=DataError("API down")
        )
        mocker.patch.object(scorer, "_fetch_rss_headlines", return_value=[])
        signal = scorer.score()
        assert signal.source == "sentiment"

    def test_live_fetch_produces_signal(self, scorer, mocker):
        mocker.patch.object(
            scorer,
            "_fetch_newsapi_headlines",
            return_value=["Markets rally strongly", "Stocks surge on earnings beat"],
        )
        mocker.patch.object(scorer, "_fetch_rss_headlines", return_value=[])
        signal = scorer.score()
        assert signal.source == "sentiment"
        assert signal.direction in ("long", "strong_long", "neutral")


class TestRealFinBERTUnavailable:
    def test_load_model_raises_data_error_without_transformers(self):
        """SentimentScorer raises DataError gracefully if transformers absent."""
        scorer = SentimentScorer()
        try:
            import transformers  # noqa: F401
            pytest.skip("transformers is installed — skip this test")
        except ImportError:
            from core.exceptions import DataError
            with pytest.raises(DataError, match="transformers"):
                scorer._load_model()
