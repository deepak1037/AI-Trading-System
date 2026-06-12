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
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

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
# Browser UA — several feeds (Yahoo, Cloudflare-fronted trumpstruth) return 429
# or block feedparser's default agent.
_FEED_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _strip_html(raw: str) -> str:
    """Reduce an RSS HTML body to plain text (tags removed, entities decoded)."""
    import html as _html

    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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
        # TTL fetch caches. This is the fix for "stale headlines": the OLD code
        # kept a session-lifetime set of seen hashes and filtered every headline
        # it had EVER seen, so after the first tick sentiment went permanently
        # neutral. We now cache by TIME, not identity-forever.
        #
        # Two sources, two cadences:
        #   • NewsAPI — quota-limited (free tier ~100 req/day, articles delayed),
        #     so it is refreshed only once per SENTIMENT_CACHE_TTL_SECONDS.
        #   • RSS (Yahoo/MarketWatch) — free + real-time, refreshed every poll so
        #     the sub-minute geopolitical detector sees breaking news at once.
        self._newsapi_cache: list[str] = []
        self._newsapi_ts: Optional[float] = None
        self._rss_cache: list[str] = []
        self._rss_ts: Optional[float] = None
        # Presidential (Truth Social) post cache, refreshed on the 60s poll.
        self._pres_cache: list[dict] = []
        self._pres_ts: Optional[float] = None

    def ensure_loaded(self) -> None:
        """Pre-load FinBERT now (e.g. at startup) so the first score() is fast."""
        self._load_model()

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
        """Fetch the FRESHEST headlines from NewsAPI.

        Uses ``get_everything`` sorted by ``publishedAt`` within a recent
        lookback window so we get CURRENT news (the old ``get_top_headlines``
        business feed updates slowly and lagged behind breaking events). Falls
        back to top-headlines if ``get_everything`` returns nothing.
        """
        if not settings.NEWS_API_KEY:
            # TODO: NEWS_API_KEY not configured — add to .env
            logger.warning("NEWS_API_KEY not set — skipping NewsAPI fetch")
            return []
        try:
            from newsapi import NewsApiClient  # type: ignore[import-untyped]

            client = NewsApiClient(api_key=settings.NEWS_API_KEY)
            from_ts = (
                datetime.now(tz=timezone.utc)
                - timedelta(hours=settings.SENTIMENT_NEWS_LOOKBACK_HOURS)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            resp = client.get_everything(
                q=query,
                language="en",
                sort_by="publishedAt",  # newest first — current news
                from_param=from_ts,
                page_size=n,
            )
            articles = resp.get("articles", [])
            titles = [a["title"] for a in articles if a.get("title")]
            if not titles:
                resp = client.get_top_headlines(
                    language="en", category="business", page_size=n
                )
                titles = [
                    a["title"] for a in resp.get("articles", []) if a.get("title")
                ]
            return titles
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
                feed = feedparser.parse(url, agent=_FEED_UA)
                for entry in feed.entries[:n_per_feed]:
                    title = getattr(entry, "title", None)
                    if title:
                        headlines.append(title)
            except Exception as exc:
                logger.warning("RSS fetch failed for %s: %s", url, exc)
        return headlines

    def _dedup_batch(self, headlines: list[str]) -> list[str]:
        """Drop exact duplicates WITHIN the current batch, preserving order.

        Unlike the old cross-tick dedup, this does NOT remember headlines across
        ticks — so a headline still present in the news on the next refresh is
        re-scored, keeping sentiment aligned with the CURRENT news set.
        """
        seen: set[str] = set()
        out: list[str] = []
        for h in headlines:
            key = hashlib.md5(h.encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                out.append(h)
        return out

    @staticmethod
    def _stale(ts: Optional[float], ttl: float) -> bool:
        """True if ``ts`` is unset or older than ``ttl`` seconds (monotonic)."""
        return ts is None or (time.monotonic() - ts) >= ttl

    def fetch_headlines(
        self, query: str = "stock market economy", force: bool = False
    ) -> list[str]:
        """Return current headlines (NewsAPI TTL-gated + fresh RSS), deduped.

        Args:
            query: NewsAPI query string.
            force: Refresh the real-time RSS source immediately (used by the
                sub-minute geopolitical poll). NewsAPI stays TTL-gated regardless
                — its free tier is rate-limited and delayed, so polling it every
                minute would both exhaust the quota and add no fresh signal.
        """
        # NewsAPI: refresh only once per TTL (quota protection).
        if self._stale(self._newsapi_ts, settings.SENTIMENT_CACHE_TTL_SECONDS):
            try:
                titles = self._fetch_newsapi_headlines(query=query)
                if titles:
                    self._newsapi_cache = titles
                self._newsapi_ts = time.monotonic()
            except DataError as exc:
                logger.warning("NewsAPI unavailable: %s", exc)

        # RSS: free + real-time. Refresh on force, else at a 60s floor so the
        # routine tick doesn't re-parse feeds every few seconds during macro.
        if force or self._stale(self._rss_ts, min(60, settings.SENTIMENT_CACHE_TTL_SECONDS)):
            rss = self._fetch_rss_headlines()
            if rss:
                self._rss_cache = rss
            self._rss_ts = time.monotonic()

        batch = self._dedup_batch(self._newsapi_cache + self._rss_cache)
        logger.debug(
            "SentimentScorer: %d headlines (newsapi=%d rss=%d)",
            len(batch), len(self._newsapi_cache), len(self._rss_cache),
        )
        return batch

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
        A geopolitical shock (keyword + sharp negative sentiment) overrides the
        routine score with an immediate risk-off signal.
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

        if settings.GEOPOLITICAL_WATCH:
            shock = self._detect_geopolitical_shock(classified)
            if shock is not None:
                return self._build_shock_signal(shock, len(classified))

        return self._signal_from_classified(classified)

    def _signal_from_classified(self, classified: list[dict]) -> Signal:
        """Build the routine aggregate sentiment Signal from classified headlines."""
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
        """Fetch current headlines (TTL-cached) and score them. Full live pipeline."""
        headlines = self.fetch_headlines(query=query)
        if not headlines:
            return Signal(
                direction="neutral",
                confidence=20,
                source="sentiment",
                timestamp=datetime.now(tz=timezone.utc),
                metadata={"reason": "no_headlines"},
            )
        return self.score_headlines(headlines)

    # ── Geopolitical / event-shock detection ──────────────────────────────────

    def _matched_keywords(self, text: str) -> list[str]:
        """Return configured geopolitical keywords present in ``text`` (case-insensitive)."""
        lower = text.lower()
        return [kw for kw in settings.GEOPOLITICAL_KEYWORDS if kw.lower() in lower]

    def _detect_geopolitical_shock(self, classified: list[dict]) -> Optional[dict]:
        """Detect a geopolitical risk-off shock from classified headlines.

        Fires when one or more headlines contain a geopolitical keyword AND the
        sentiment of those matched headlines is sharply negative
        (avg < GEOPOLITICAL_SENTIMENT_DROP). Returns a dict of evidence, or None.
        """
        matched: list[dict] = []
        keywords: set[str] = set()
        for r in classified:
            hits = self._matched_keywords(r["text"])
            if hits:
                matched.append(r)
                keywords.update(hits)
        if not matched:
            return None

        # Require a cluster of geopolitical headlines — a single passing mention
        # of "China"/"war"/"strike" in routine economic news is not a shock.
        if len(matched) < settings.GEOPOLITICAL_MIN_HEADLINES:
            return None

        matched_scores = [_sentiment_to_score(r["label"], r["score"]) for r in matched]
        matched_avg = sum(matched_scores) / len(matched_scores)
        # Fire only when the geopolitical coverage is, ON BALANCE, sharply
        # negative. Averaging (not worst-case) is deliberate: it rejects normal
        # days where one negative headline sits among mostly neutral/positive
        # geo mentions (which previously caused false shocks).
        if matched_avg >= settings.GEOPOLITICAL_SENTIMENT_DROP:
            return None

        return {
            "matched_avg": round(matched_avg, 4),
            "worst_score": round(min(matched_scores), 4),
            "keywords": sorted(keywords),
            "headlines": [r["text"] for r in matched[:5]],
            "matched_count": len(matched),
        }

    def _build_shock_signal(self, shock: dict, headline_count: int) -> Signal:
        """Build the GEOPOLITICAL_SHOCK risk-off Signal (direction=short)."""
        logger.warning(
            "GEOPOLITICAL_SHOCK detected — keywords=%s matched_avg=%.3f "
            "(%d/%d headlines): %s",
            shock["keywords"], shock["matched_avg"], shock["matched_count"],
            headline_count, shock["headlines"][:2],
        )
        return Signal(
            direction="short",
            confidence=settings.GEOPOLITICAL_SHOCK_CONFIDENCE,
            source="sentiment",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "event": "GEOPOLITICAL_SHOCK",
                "geopolitical_shock": True,
                "keywords": shock["keywords"],
                "matched_avg": shock["matched_avg"],
                "matched_count": shock["matched_count"],
                "headline_count": headline_count,
                "sample_headlines": shock["headlines"],
            },
        )

    def check_geopolitical(self, query: str = "stock market economy") -> Optional[Signal]:
        """Force-fetch current headlines and return a shock Signal, or None.

        Used by the dedicated sub-minute poll: ``force=True`` bypasses the TTL so
        breaking news isn't masked by a still-warm cache. Never raises — returns
        None on any failure so the poll loop is safe.
        """
        if not settings.GEOPOLITICAL_WATCH:
            return None
        try:
            headlines = self.fetch_headlines(query=query, force=True)
            if not headlines:
                return None
            classified = self._classify(headlines)
            if not classified:
                return None
            shock = self._detect_geopolitical_shock(classified)
            if shock is None:
                return None
            return self._build_shock_signal(shock, len(classified))
        except Exception as exc:  # poll must never crash the scheduler
            logger.warning("check_geopolitical failed: %s", exc)
            return None

    # ── Presidential / Truth Social monitoring ────────────────────────────────

    def _fetch_presidential_posts(self, force: bool = False) -> list[dict]:
        """Fetch recent presidential posts (text, link, published) from the feeds.

        TTL-cached on the same cadence as RSS. Truth Social's own .rss is dead
        (serves the SPA), so PRESIDENTIAL_RSS_URLS points at trumpstruth.org,
        which republishes his posts as valid RSS.
        """
        if not force and not self._stale(
            self._pres_ts, min(60, settings.SENTIMENT_CACHE_TTL_SECONDS)
        ):
            return self._pres_cache
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser not installed — presidential feed disabled")
            return self._pres_cache

        posts: list[dict] = []
        for url in settings.PRESIDENTIAL_RSS_URLS:
            try:
                feed = feedparser.parse(url, agent=_FEED_UA)
                for entry in feed.entries:
                    raw = entry.get("summary") or entry.get("title") or ""
                    text = _strip_html(raw)
                    if not text:
                        continue
                    published = None
                    pp = entry.get("published_parsed")
                    if pp is not None:
                        # struct_time is UTC for RSS pubDate.
                        published = datetime(
                            pp.tm_year, pp.tm_mon, pp.tm_mday,
                            pp.tm_hour, pp.tm_min, pp.tm_sec,
                            tzinfo=timezone.utc,
                        )
                    posts.append(
                        {"text": text, "link": entry.get("link"), "published": published}
                    )
            except Exception as exc:
                logger.warning("Presidential feed fetch failed for %s: %s", url, exc)
        if posts:
            self._pres_cache = posts
            self._pres_ts = time.monotonic()
        return self._pres_cache

    def _classify_presidential(self, text: str) -> tuple[Direction, dict]:
        """Direction from market keywords, handling de-escalation negation.

        Multi-word phrases win over substrings (so "no deal" beats "deal" and
        "tariff reduction" beats "tariffs"); a negative keyword preceded by a
        negation word ("cancelled strikes") flips bullish. Returns the direction
        and the matched-keyword evidence.
        """
        t = text.lower()
        matches: list[tuple[int, int, str, str]] = []  # (start, end, polarity, kw)
        for kw in settings.PRESIDENTIAL_POSITIVE_KEYWORDS:
            for m in re.finditer(r"\b" + re.escape(kw.lower()) + r"\b", t):
                matches.append((m.start(), m.end(), "pos", kw))
        for kw in settings.PRESIDENTIAL_NEGATIVE_KEYWORDS:
            for m in re.finditer(r"\b" + re.escape(kw.lower()) + r"\b", t):
                matches.append((m.start(), m.end(), "neg", kw))

        # Greedy longest-first, non-overlapping (resolves "no deal" vs "deal").
        matches.sort(key=lambda c: c[1] - c[0], reverse=True)
        used: list[tuple[int, int]] = []
        negators = [w.lower() for w in settings.PRESIDENTIAL_NEGATION_WORDS]
        positive: list[str] = []
        negative: list[str] = []
        for start, end, pol, kw in matches:
            if any(not (end <= us or start >= ue) for us, ue in used):
                continue
            used.append((start, end))
            if pol == "neg":
                window = t[max(0, start - 30):start]
                if any(n in window for n in negators):
                    positive.append(f"(de-escalation) {kw}")  # bullish
                else:
                    negative.append(kw)
            else:
                positive.append(kw)

        if len(positive) > len(negative):
            direction: Direction = "long"
        elif len(negative) > len(positive):
            direction = "short"
        else:
            direction = "neutral"  # tie or no signal — still a heads-up if matched
        return direction, {"positive": positive, "negative": negative}

    def check_presidential(self, now: Optional[datetime] = None) -> list[Signal]:
        """Return PRESIDENTIAL_STATEMENT signals for FRESH market-relevant posts.

        Fires on a market-keyword match REGARDLESS of FinBERT sentiment (a
        presidential post is itself the event). Only posts published within
        PRESIDENTIAL_FRESH_MINUTES are considered, so a backlog isn't replayed.
        Never raises — returns [] on any failure (poll-safe).
        """
        if not settings.PRESIDENTIAL_WATCH:
            return []
        try:
            posts = self._fetch_presidential_posts(force=True)
        except Exception as exc:  # poll must never crash the scheduler
            logger.warning("check_presidential failed: %s", exc)
            return []

        now = now or datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(minutes=settings.PRESIDENTIAL_FRESH_MINUTES)
        signals: list[Signal] = []
        for post in posts:
            published = post.get("published")
            if published is not None and published < cutoff:
                continue  # stale — react to breaking posts only
            direction, evidence = self._classify_presidential(post["text"])
            if not evidence["positive"] and not evidence["negative"]:
                continue  # no market keywords — ignore
            logger.warning(
                "PRESIDENTIAL_STATEMENT — %s (pos=%s neg=%s): %s",
                direction, evidence["positive"], evidence["negative"],
                post["text"][:120],
            )
            signals.append(
                Signal(
                    direction=direction,
                    confidence=settings.PRESIDENTIAL_SHOCK_CONFIDENCE,
                    source="sentiment",
                    timestamp=now,
                    metadata={
                        "event": "PRESIDENTIAL_STATEMENT",
                        "presidential": True,
                        "post_text": post["text"][:500],
                        "link": post.get("link"),
                        "published": published.isoformat() if published else None,
                        "positive_keywords": evidence["positive"],
                        "negative_keywords": evidence["negative"],
                    },
                )
            )
        return signals


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
