"""Tests for the two June-9 fixes:

  1. Stale headlines — sentiment must reflect CURRENT news every tick, not go
     permanently neutral after the first tick (old _seen_hashes bug).
  2. Geopolitical shock detector — war/strike keywords + sharp negative
     sentiment → short, confidence=70, routed to Discord #alerts.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from signals.sentiment_scorer import MockSentimentScorer


@pytest.fixture
def scorer() -> MockSentimentScorer:
    return MockSentimentScorer()


# ---------------------------------------------------------------------------
# FAILURE 1 — stale headlines
# ---------------------------------------------------------------------------
def test_sentiment_not_stuck_neutral_across_ticks(scorer) -> None:
    """The same bearish headline must score bearish on EVERY tick, not neutral.

    Reproduces the old bug: cross-tick dedup made tick 2+ return neutral
    'no_fresh_headlines'. Now headlines are re-scored each tick.
    """
    bearish = ["Stocks plunge as data comes in weak", "Shares fall on miss"]
    with patch.object(scorer, "_fetch_newsapi_headlines", return_value=bearish), \
         patch.object(scorer, "_fetch_rss_headlines", return_value=[]):
        s1 = scorer.score()
        s2 = scorer.score()
        s3 = scorer.score()
    for s in (s1, s2, s3):
        assert s.direction in ("short", "strong_short")
        assert s.metadata.get("reason") != "no_fresh_headlines"
    assert s1.direction == s2.direction == s3.direction


def test_newsapi_ttl_gated_rss_realtime(scorer) -> None:
    """NewsAPI fetched once (TTL), RSS refreshed on force (real-time path)."""
    calls = {"news": 0, "rss": 0}

    def fake_news(query="x", n=20):
        calls["news"] += 1
        return ["NewsAPI biz headline"]

    def fake_rss(n_per_feed=10):
        calls["rss"] += 1
        return ["RSS live headline"]

    with patch.object(scorer, "_fetch_newsapi_headlines", side_effect=fake_news), \
         patch.object(scorer, "_fetch_rss_headlines", side_effect=fake_rss):
        scorer.fetch_headlines()             # initial
        scorer.fetch_headlines()             # within TTL → no new news fetch
        scorer.fetch_headlines(force=True)   # geopolitical poll → RSS refetch
    assert calls["news"] == 1               # quota-protected
    assert calls["rss"] >= 2                # refreshed on force


def test_transient_fetch_failure_keeps_prior_batch(scorer) -> None:
    """If a refresh returns nothing, the last good headlines are retained."""
    with patch.object(scorer, "_fetch_newsapi_headlines", return_value=["Good headline"]), \
         patch.object(scorer, "_fetch_rss_headlines", return_value=[]):
        first = scorer.fetch_headlines()
    assert first == ["Good headline"]
    # Force a refresh where NewsAPI is still TTL-cached and RSS yields nothing.
    with patch.object(scorer, "_fetch_rss_headlines", return_value=[]):
        again = scorer.fetch_headlines(force=True)
    assert "Good headline" in again


# ---------------------------------------------------------------------------
# FAILURE 2 — geopolitical shock
# ---------------------------------------------------------------------------
def test_geopolitical_shock_fires(scorer) -> None:
    headlines = [
        "US launches military strikes on Iran, markets plunge",
        "Stocks fall sharply as war escalates",
    ]
    sig = scorer.score_headlines(headlines)
    assert sig.direction == "short"
    assert sig.confidence == 70
    assert sig.metadata["event"] == "GEOPOLITICAL_SHOCK"
    assert sig.metadata["geopolitical_shock"] is True
    assert any(k in sig.metadata["keywords"] for k in ("Iran", "war", "military"))


def test_no_false_shock_on_routine_news(scorer) -> None:
    """A single negative geo mention amid neutral/positive news must NOT fire.

    Regression for the live false-positive: 'China Export Growth Accelerates'
    (positive) + one routine negative headline averaged ~0 yet wrongly fired a
    shock under the old worst-case override.
    """
    headlines = [
        "China Export Growth Accelerates in May on AI demand, shipments surge",
        "Treasury market steady as traders weigh rate path",
        "Some military supplier misses earnings, shares fall",
    ]
    sig = scorer.score_headlines(headlines)
    assert sig.metadata.get("event") != "GEOPOLITICAL_SHOCK"


def test_single_geo_mention_below_min_no_shock(scorer) -> None:
    """One matched headline (< GEOPOLITICAL_MIN_HEADLINES) does not fire."""
    headlines = ["China factory output falls", "Apple unveils new product"]
    sig = scorer.score_headlines(headlines)
    assert sig.metadata.get("event") != "GEOPOLITICAL_SHOCK"


def test_no_shock_without_keywords(scorer) -> None:
    headlines = ["Stocks drop on weak earnings", "Tech shares fall on guidance"]
    sig = scorer.score_headlines(headlines)
    assert sig.metadata.get("event") != "GEOPOLITICAL_SHOCK"
    assert sig.direction in ("short", "strong_short")  # normal bearish, not shock


def test_no_shock_when_geo_sentiment_mild(scorer) -> None:
    """Keywords present but positive/mild sentiment → no shock."""
    headlines = ["China trade talks resume, markets rally", "Iran deal lifts stocks"]
    sig = scorer.score_headlines(headlines)
    assert sig.metadata.get("event") != "GEOPOLITICAL_SHOCK"


def test_check_geopolitical_returns_signal(scorer) -> None:
    geo = ["US military strikes Iran, stocks plunge", "Markets fall on war fears"]
    with patch.object(scorer, "_fetch_newsapi_headlines", return_value=[]), \
         patch.object(scorer, "_fetch_rss_headlines", return_value=geo):
        sig = scorer.check_geopolitical()
    assert sig is not None
    assert sig.direction == "short"
    assert sig.confidence == 70


def test_check_geopolitical_none_when_disabled(scorer) -> None:
    with patch("config.settings.settings.GEOPOLITICAL_WATCH", False):
        assert scorer.check_geopolitical() is None


def test_check_geopolitical_quiet_market_returns_none(scorer) -> None:
    calm = ["Stocks edge higher in quiet trading", "Markets steady ahead of data"]
    with patch.object(scorer, "_fetch_newsapi_headlines", return_value=[]), \
         patch.object(scorer, "_fetch_rss_headlines", return_value=calm):
        assert scorer.check_geopolitical() is None
