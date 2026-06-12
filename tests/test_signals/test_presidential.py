"""Tests for presidential / Truth Social monitoring in SentimentScorer.

Covers the live-miss case ("I cancelled Iran strikes" → LONG), keyword-driven
direction with phrase precedence + negation, the freshness window, and that a
match fires regardless of FinBERT sentiment (keyword-only path, no model).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from signals.sentiment_scorer import SentimentScorer


@pytest.fixture
def scorer() -> SentimentScorer:
    return SentimentScorer()


# ---------------------------------------------------------------------------
# Classification — direction, phrase precedence, de-escalation negation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("I cancelled Iran strikes. Peace is at hand!", "long"),   # the live miss
        ("We are launching massive STRIKES on Iran tonight", "short"),
        ("Just signed a historic TRADE DEAL with China", "long"),
        ("There will be NO DEAL. Terminated.", "short"),           # 'no deal' > 'deal'
        ("Imposing 50% TARIFFS on all imports", "short"),
        ("Announcing a major TARIFF REDUCTION for autos", "long"),  # phrase > 'tariff'
        ("We have lifted all sanctions on Russia", "long"),         # de-escalation
        ("Ceasefire agreement reached in the region", "long"),
        ("Great rally today, tremendous crowd, MAGA!", "neutral"),  # no market kw
    ],
)
def test_presidential_classification(scorer, text, expected) -> None:
    direction, _ = scorer._classify_presidential(text)
    assert direction == expected


def test_cancelled_strikes_is_bullish_evidence(scorer) -> None:
    """The de-escalation case records the flip in evidence, not as a negative."""
    direction, ev = scorer._classify_presidential("I cancelled the strikes on Iran")
    assert direction == "long"
    assert ev["negative"] == []
    assert any("strikes" in p for p in ev["positive"])


# ---------------------------------------------------------------------------
# check_presidential — freshness, firing, metadata
# ---------------------------------------------------------------------------
def _post(text: str, minutes_ago: float, now: datetime) -> dict:
    return {
        "text": text,
        "link": "https://trumpstruth.org/statuses/1",
        "published": now - timedelta(minutes=minutes_ago),
    }


def test_fresh_market_post_fires(scorer) -> None:
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    posts = [_post("I cancelled Iran strikes, peace now", minutes_ago=2, now=now)]
    with patch.object(scorer, "_fetch_presidential_posts", return_value=posts):
        sigs = scorer.check_presidential(now=now)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.direction == "long"
    assert s.confidence == 80
    assert s.metadata["event"] == "PRESIDENTIAL_STATEMENT"
    assert s.metadata["presidential"] is True
    assert "cancelled Iran strikes" in s.metadata["post_text"]
    assert s.metadata["link"].endswith("/1")


def test_stale_post_does_not_fire(scorer) -> None:
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    # 60 min old, default fresh window is 15 min.
    posts = [_post("Massive new tariffs on China", minutes_ago=60, now=now)]
    with patch.object(scorer, "_fetch_presidential_posts", return_value=posts):
        assert scorer.check_presidential(now=now) == []


def test_non_market_post_does_not_fire(scorer) -> None:
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    posts = [_post("Tremendous rally tonight, thank you!", minutes_ago=1, now=now)]
    with patch.object(scorer, "_fetch_presidential_posts", return_value=posts):
        assert scorer.check_presidential(now=now) == []


def test_negative_post_fires_short(scorer) -> None:
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    posts = [_post("We are imposing severe sanctions and a blockade", 1, now)]
    with patch.object(scorer, "_fetch_presidential_posts", return_value=posts):
        sigs = scorer.check_presidential(now=now)
    assert sigs and sigs[0].direction == "short"


def test_disabled_returns_empty(scorer) -> None:
    with patch("config.settings.settings.PRESIDENTIAL_WATCH", False):
        assert scorer.check_presidential() == []


def test_fetch_failure_is_safe(scorer) -> None:
    with patch.object(scorer, "_fetch_presidential_posts", side_effect=RuntimeError("boom")):
        assert scorer.check_presidential() == []


def test_multiple_fresh_posts_each_fire(scorer) -> None:
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
    posts = [
        _post("Signed a great trade deal", 1, now),
        _post("New tariffs on the EU", 3, now),
        _post("Nice weather today", 2, now),  # no keyword → ignored
    ]
    with patch.object(scorer, "_fetch_presidential_posts", return_value=posts):
        sigs = scorer.check_presidential(now=now)
    assert len(sigs) == 2
    assert {s.direction for s in sigs} == {"long", "short"}
