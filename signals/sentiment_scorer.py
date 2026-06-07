"""NLP sentiment scorer using FinBERT (Day 3).

Pulls headlines from NewsAPI + RSS feeds, runs them through the
ProsusAI/finbert model, and produces a Signal with the aggregate
bullish/bearish/neutral probability.

FinBERT is loaded lazily on first call so the module can be imported
without a GPU / large model download. If the model isn't available,
a DataError is raised with a clear TODO message.

TODO: Install transformers + torch to enable FinBERT:
    pip install transformers torch
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from signals.signal_schema import Direction, Signal

logger = get_logger(__name__)

_FINBERT_MODEL = "ProsusAI/finbert"
_RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline",
    "https://www.marketwatch.com/rss/topstories",
]


def _sentiment_to_score(label: str, prob: float) -> float:
    """Convert FinBERT label+prob to a [-1, +1] score."""
    if label == "positive":
        return prob
    elif label == "negative":
        return -prob
    return 0.0


class SentimentScorer:
    """Aggregate NLP sentiment scorer for financial headlines.

    Lazy-loads FinBERT on first ``score()`` call. Falls back to a neutral
    signal (with a warning) if the model or transformers package is absent,
    so the rest of the system can continue building without it.
    """

    def __init__(self) -> None:
        self._pipeline = None
        self._seen_hashes: set[str] = set()

    def _load_model(self) -> None:
        if self._pipeline is not None:
            return
        try:
            from transformers import pipeline as hf_pipeline  # type: ignore[import-untyped]

            self._pipeline = hf_pipeline(
                "text-classification",
                model=_FINBERT_MODEL,
                tokenizer=_FINBERT_MODEL,
                top_k=None,
                device=-1,  # CPU; set to 0 for CUDA
                truncation=True,
                max_length=512,
            )
            logger.info("FinBERT model loaded: %s", _FINBERT_MODEL)
        except ImportError:
            # TODO: Install transformers and torch for live FinBERT inference
            raise DataError(
                "transformers/torch not installed — FinBERT unavailable. "
                "Run: pip install transformers torch"
            )
        except Exception as exc:
            raise DataError(f"Failed to load FinBERT: {exc}") from exc

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
    )
    def _fetch_newsapi_headlines(self, query: str = "stock market economy", n: int = 20) -> list[str]:
        """Fetch headlines from NewsAPI."""
        if not settings.NEWS_API_KEY:
            # TODO: NEWS_API_KEY not configured — add to .env
            logger.warning("NEWS_API_KEY not set — skipping NewsAPI fetch")
            return []
        try:
            from newsapi import NewsApiClient  # type: ignore[import-untyped]

            client = NewsApiClient(api_key=settings.NEWS_API_KEY)
            resp = client.get_top_headlines(language="en", category="business", page_size=n)
            articles = resp.get("articles", [])
            return [a["title"] for a in articles if a.get("title")]
        except Exception as exc:
            raise DataError(f"NewsAPI fetch failed: {exc}") from exc

    def _fetch_rss_headlines(self, n_per_feed: int = 10) -> list[str]:
        """Fetch headlines from RSS feeds."""
        headlines: list[str] = []
        try:
            import feedparser  # type: ignore[import-untyped]
        except ImportError:
            # feedparser is optional — skip RSS if not installed
            return headlines

        for url in _RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:n_per_feed]:
                    title = getattr(entry, "title", None)
                    if title:
                        headlines.append(title)
            except Exception as exc:
                logger.warning("RSS fetch failed for %s: %s", url, exc)
        return headlines

    def _deduplicate(self, headlines: list[str]) -> list[str]:
        """Return only headlines not seen in this session."""
        fresh: list[str] = []
        for h in headlines:
            h_hash = hashlib.md5(h.encode()).hexdigest()
            if h_hash not in self._seen_hashes:
                self._seen_hashes.add(h_hash)
                fresh.append(h)
        return fresh

    def _classify(self, headlines: list[str]) -> list[dict]:
        """Run headlines through FinBERT. Returns list of {label, score} dicts."""
        self._load_model()
        results = []
        for headline in headlines:
            try:
                preds = self._pipeline(headline)
                if preds:
                    # top_k=None returns a list of dicts; pick highest prob
                    best = max(preds[0], key=lambda x: x["score"])
                    results.append({"text": headline, "label": best["label"], "score": best["score"]})
            except Exception as exc:
                logger.warning("FinBERT inference failed for headline %r: %s", headline[:50], exc)
        return results

    def score_headlines(self, headlines: list[str]) -> Signal:
        """Produce a Signal from a list of already-fetched headlines.

        Useful in tests and backtests where headlines are provided directly.
        """
        if not headlines:
            return Signal(
                direction="neutral",
                confidence=0,
                source="sentiment",
                timestamp=datetime.now(tz=timezone.utc),
                metadata={"headline_count": 0, "reason": "no_headlines"},
            )

        classified = self._classify(headlines)
        if not classified:
            return Signal(
                direction="neutral",
                confidence=30,
                source="sentiment",
                timestamp=datetime.now(tz=timezone.utc),
                metadata={"headline_count": len(headlines), "reason": "classification_failed"},
            )

        scores = [_sentiment_to_score(r["label"], r["score"]) for r in classified]
        avg_score = sum(scores) / len(scores)
        abs_score = abs(avg_score)

        # Map to direction + confidence
        threshold = settings.SENTIMENT_THRESHOLD  # default 0.3
        if abs_score >= threshold * 2:
            direction: Direction = "strong_long" if avg_score > 0 else "strong_short"
            confidence = min(95, int(55 + abs_score * 100))
        elif abs_score >= threshold:
            direction = "long" if avg_score > 0 else "short"
            confidence = min(75, int(40 + abs_score * 80))
        else:
            direction = "neutral"
            confidence = max(10, int(abs_score * 60))

        bullish = sum(1 for r in classified if r["label"] == "positive")
        bearish = sum(1 for r in classified if r["label"] == "negative")
        neutral_count = len(classified) - bullish - bearish

        logger.info(
            "SentimentScorer: n=%d avg_score=%.3f direction=%s confidence=%d "
            "(bullish=%d bearish=%d neutral=%d)",
            len(classified), avg_score, direction, confidence,
            bullish, bearish, neutral_count,
        )

        return Signal(
            direction=direction,
            confidence=confidence,
            source="sentiment",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "headline_count": len(classified),
                "avg_score": round(avg_score, 4),
                "bullish": bullish,
                "bearish": bearish,
                "neutral": neutral_count,
                "threshold": threshold,
            },
        )

    def score(self, query: str = "stock market economy") -> Signal:
        """Fetch headlines and score them. Full live pipeline."""
        headlines: list[str] = []

        try:
            news = self._fetch_newsapi_headlines(query=query)
            headlines.extend(news)
        except DataError as exc:
            logger.warning("NewsAPI unavailable: %s", exc)

        rss = self._fetch_rss_headlines()
        headlines.extend(rss)

        fresh = self._deduplicate(headlines)
        logger.debug("SentimentScorer: %d total, %d fresh headlines", len(headlines), len(fresh))

        if not fresh:
            return Signal(
                direction="neutral",
                confidence=20,
                source="sentiment",
                timestamp=datetime.now(tz=timezone.utc),
                metadata={"reason": "no_fresh_headlines"},
            )

        return self.score_headlines(fresh)


class MockSentimentScorer(SentimentScorer):
    """Test double: classifies headlines by keyword matching, no model needed."""

    _BULLISH_WORDS = {"surge", "rally", "gain", "rise", "beat", "strong", "bull", "up"}
    _BEARISH_WORDS = {"drop", "fall", "plunge", "miss", "weak", "bear", "down", "cut"}

    def _classify(self, headlines: list[str]) -> list[dict]:
        results = []
        for h in headlines:
            lower = h.lower()
            bull_hits = sum(1 for w in self._BULLISH_WORDS if w in lower)
            bear_hits = sum(1 for w in self._BEARISH_WORDS if w in lower)
            if bull_hits > bear_hits:
                results.append({"text": h, "label": "positive", "score": 0.8})
            elif bear_hits > bull_hits:
                results.append({"text": h, "label": "negative", "score": 0.8})
            else:
                results.append({"text": h, "label": "neutral", "score": 0.6})
        return results


__all__ = ["SentimentScorer", "MockSentimentScorer"]
