"""Real-time presidential (Truth Social) post monitor — Phase 2b.

Upgrades Trump/presidential detection from 60s RSS polling to near-real-time by
trying multiple data sources in priority order and using the first that
connects:

    1. ScrapeCreators  — REST, fast polling (~10s lag)
    2. TweetStream     — WebSocket, true real-time push (~5s lag)
    3. Apify           — REST with webhook (~15s lag)
    4. RSS (trumpstruth.org) — always-available fallback (~60s lag)

When a market-moving post is detected the monitor classifies direction
(LONG/SHORT), suggests affected instruments, fires a Discord alert through
``AlertEngine`` (never Discord directly), and logs to ``presidential_signals``.

Design guarantees:
  • Runs in a daemon thread — never blocks the main process.
  • Never raises out of the polling/stream loop — a source failure degrades to
    the next source, and if everything fails the system keeps running without
    presidential monitoring (just a logged warning).
  • Runs 24/7 including weekends (a Sunday post can gap Monday's open).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from config.settings import settings
from core.logger import get_logger

if TYPE_CHECKING:
    from alerts.alert_engine import AlertEngine

logger = get_logger(__name__)

_SCRAPECREATORS_URL = "https://api.scrapecreators.com/v1/truthsocial/user/posts"
_FEED_UA = "Mozilla/5.0 (compatible; AI-Trading-System/1.0)"

# Per-source human-readable latency, surfaced in the alert so the trader knows
# how fresh the signal is.
_SOURCE_LAG = {
    "tweetstream": "~5 seconds",
    "scrapecreators": "~10 seconds",
    "apify": "~15 seconds",
    "rss": "~60 seconds",
}

# HTTP statuses that mean a source is permanently unusable (auth / quota /
# payment-required / forbidden) — retrying won't help, switch sources immediately.
_FATAL_HTTP = frozenset({401, 402, 403})
# After this many consecutive transient failures, give up on a source and switch.
_MAX_CONSECUTIVE_FAILURES = 3


class _SourceExhausted(Exception):
    """A data source is permanently unusable — fall back to the next one now."""


def _normalize_post(post: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ScrapeCreators/Apify post dict to the common shape."""
    return {
        "post_id": str(post.get("id", post.get("url", ""))),
        "text": str(post.get("content", post.get("text", ""))),
        "created_at": str(post.get("created_at", post.get("timestamp", ""))),
        "url": str(post.get("url", "")),
    }


class PresidentialSignal(BaseModel):
    """A classified market-moving presidential post."""

    direction: str  # "long" | "short"
    confidence: int = 80
    keywords: list[str] = Field(default_factory=list)
    suggested_instruments: list[str] = Field(default_factory=list)


class RealTimePresidentialMonitor:
    """Multi-source real-time monitor for market-moving presidential posts."""

    def __init__(self, alert_engine: AlertEngine | None = None) -> None:
        self.alert_engine = alert_engine
        self.seen_post_ids: set[str] = set()  # dedup across the process lifetime
        self.active_source: str | None = None
        self._ws: Any = None  # websocket.WebSocketApp once connected
        self._running: bool = False
        self._thread: threading.Thread | None = None

    # ── Source detection ──────────────────────────────────────────────────────
    def detect_active_source(self) -> str:
        """Try each configured source in priority order; return the first usable.

        Never raises — falls through to ``"rss"`` (always available). Only
        sources with a configured API key are probed; RSS needs no key.
        """
        for source in settings.PRESIDENTIAL_SOURCE_PRIORITY:
            try:
                if source == "scrapecreators":
                    if not self._has_key(settings.SCRAPECREATORS_API_KEY):
                        continue  # no key → never probe ScrapeCreators
                    if self._fetch_scrapecreators() is not None:
                        logger.info("Presidential monitor: using ScrapeCreators")
                        return "scrapecreators"
                elif source == "tweetstream":
                    if not self._has_key(settings.TWEETSTREAM_API_KEY):
                        continue
                    if self._test_tweetstream_auth():
                        logger.info("Presidential monitor: using TweetStream WebSocket")
                        return "tweetstream"
                elif source == "apify":
                    if not self._has_key(settings.APIFY_API_KEY):
                        continue
                    if self._fetch_apify() is not None:
                        logger.info("Presidential monitor: using Apify")
                        return "apify"
                elif source == "rss":
                    logger.info("Presidential monitor: using RSS fallback (trumpstruth.org)")
                    return "rss"
            except _SourceExhausted as exc:
                logger.warning("Presidential source %s exhausted: %s", source, exc)
            except Exception as exc:
                logger.warning("Presidential source %s unavailable: %s", source, exc)

        logger.info("Presidential monitor: using RSS fallback (trumpstruth.org)")
        return "rss"

    # ── Source A: ScrapeCreators (REST polling) ───────────────────────────────
    def _fetch_scrapecreators(self) -> list[dict[str, Any]] | None:
        """Poll ScrapeCreators for the latest posts. None on hard error."""
        import httpx

        response = httpx.get(
            _SCRAPECREATORS_URL,
            params={"user_id": settings.PRESIDENTIAL_TRUMP_USER_ID},
            headers={"x-api-key": settings.SCRAPECREATORS_API_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            posts = data.get("posts", data.get("data", []))
            return list(posts) if posts is not None else []
        if response.status_code == 429:
            logger.warning("ScrapeCreators rate limited (429)")
            return []
        if response.status_code in _FATAL_HTTP:
            raise _SourceExhausted(
                f"ScrapeCreators quota exhausted ({response.status_code})"
            )
        raise RuntimeError(f"ScrapeCreators error: {response.status_code}")

    def _run_scrapecreators_loop(self) -> None:
        """Poll ScrapeCreators; returns (to trigger a source switch) on a fatal
        status (e.g. 402 quota) or after too many consecutive failures."""
        self._poll_loop("ScrapeCreators", "scrapecreators", self._fetch_scrapecreators,
                         settings.SCRAPECREATORS_POLL_SECONDS)

    def _poll_loop(self, label: str, source: str, fetch, interval: float) -> None:
        """Generic REST polling loop with fatal-error + failure-count switching.

        Returns (ending the loop so ``_run_chain`` advances to the next source)
        when ``fetch`` raises ``_SourceExhausted`` (quota/auth — switch now) or
        fails ``_MAX_CONSECUTIVE_FAILURES`` times in a row. Otherwise polls forever.
        """
        failures = 0
        while self._running:
            try:
                for post in fetch() or []:
                    self._process_post(
                        post_id=str(post.get("id", post.get("url", ""))),
                        text=str(post.get("content", post.get("text", ""))),
                        created_at=str(post.get("created_at", post.get("timestamp", ""))),
                        url=str(post.get("url", "")),
                        source=source,
                    )
                failures = 0
            except _SourceExhausted as exc:
                logger.warning("%s — falling back to next source (RSS)", exc)
                return
            except Exception as exc:
                failures += 1
                logger.error(
                    "%s loop error (%d/%d): %s",
                    label, failures, _MAX_CONSECUTIVE_FAILURES, exc,
                )
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "%s failed %d times in a row — falling back to next source",
                        label, failures,
                    )
                    return
            time.sleep(interval)

    # ── Source B: TweetStream (WebSocket) ─────────────────────────────────────
    def _test_tweetstream_auth(self) -> bool:
        """Lightweight check that the WebSocket dependency + key are present."""
        import websocket  # noqa: F401 — import is the availability probe

        return bool(settings.TWEETSTREAM_API_KEY)

    def _run_tweetstream(self) -> None:
        import websocket

        def on_open(ws: Any) -> None:
            logger.info("TweetStream WebSocket connected")
            ws.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "sources": ["truth_social"],
                        "filter": {"username": "realDonaldTrump"},
                    }
                )
            )

        def on_message(ws: Any, message: str) -> None:
            try:
                data = json.loads(message)
                if data.get("platform") != "truth_social":
                    return
                self._process_post(
                    post_id=str(data.get("id", "")),
                    text=str(data.get("text", "")),
                    created_at=str(data.get("created_at", "")),
                    url=str(data.get("url", "")),
                    source="tweetstream",
                )
            except Exception as exc:
                logger.error("TweetStream message error: %s", exc)

        def on_error(ws: Any, error: Any) -> None:
            logger.error("TweetStream error: %s", error)

        def on_close(ws: Any, code: Any, msg: Any) -> None:
            logger.warning("TweetStream disconnected — will reconnect")

        self._ws = websocket.WebSocketApp(
            settings.TWEETSTREAM_WS_URL,
            header={"Authorization": f"Bearer {settings.TWEETSTREAM_API_KEY}"},
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        while self._running:
            try:
                self._ws.run_forever(reconnect=5)
            except Exception as exc:
                logger.error("TweetStream connection failed: %s", exc)
                time.sleep(10)

    # ── Source C: Apify (REST polling) ────────────────────────────────────────
    def _fetch_apify(self) -> list[dict[str, Any]] | None:
        """Fetch the latest run's dataset items from the Apify actor."""
        import httpx

        actor = settings.APIFY_ACTOR_ID.replace("/", "~")
        url = f"https://api.apify.com/v2/acts/{actor}/runs/last/dataset/items"
        response = httpx.get(
            url,
            params={"token": settings.APIFY_API_KEY, "clean": "true"},
            timeout=15,
        )
        if response.status_code == 200:
            data = response.json()
            return list(data) if isinstance(data, list) else []
        if response.status_code in _FATAL_HTTP:
            raise _SourceExhausted(f"Apify quota exhausted ({response.status_code})")
        raise RuntimeError(f"Apify error: {response.status_code}")

    def _run_apify_loop(self) -> None:
        self._poll_loop("Apify", "apify", self._fetch_apify,
                        settings.SCRAPECREATORS_POLL_SECONDS)

    # ── Source D: RSS fallback ────────────────────────────────────────────────
    def _fetch_rss_posts(self) -> list[dict[str, Any]]:
        """Normalized recent posts from the RSS feed (newest-first as published)."""
        import feedparser

        feed = feedparser.parse(settings.PRESIDENTIAL_RSS_URLS[0], agent=_FEED_UA)
        limit = settings.PRESIDENTIAL_CATCHUP_MAX_POSTS
        return [
            {
                "post_id": str(entry.get("id", entry.get("link", ""))),
                "text": str(entry.get("summary", entry.get("title", ""))),
                "created_at": str(entry.get("published", "")),
                "url": str(entry.get("link", "")),
            }
            for entry in feed.entries[:limit]
        ]

    def _run_rss_loop(self) -> None:
        while self._running:
            try:
                for post in self._fetch_rss_posts()[:10]:
                    self._process_post(
                        post["post_id"], post["text"], post["created_at"],
                        post["url"], "rss",
                    )
            except Exception as exc:
                logger.error("RSS loop error: %s", exc)
            time.sleep(60)

    # ── Shared post pipeline ──────────────────────────────────────────────────
    def _process_post(
        self,
        post_id: str,
        text: str,
        created_at: str,
        url: str,
        source: str,
        cutoff: datetime | None = None,
    ) -> None:
        """Dedup → freshness → classify → log → alert. Never raises.

        Normal polling gates on the ``PRESIDENTIAL_FRESH_MINUTES`` window. During
        startup catch-up a ``cutoff`` is passed instead: only posts published
        strictly after it (the last-processed timestamp) are handled, so missed
        posts from an outage are replayed without flooding old history.
        """
        try:
            if not post_id or post_id in self.seen_post_ids:
                return
            self.seen_post_ids.add(post_id)

            if cutoff is not None:
                dt = self._parse_dt(created_at)
                if dt is None or dt <= cutoff:
                    return  # catch-up: skip anything not strictly newer than cutoff
            elif not self._is_recent(created_at):
                return

            signal = self._classify_post(text)
            if signal is None:
                return  # no market-relevant keywords

            self._log_to_db(post_id, text, url, signal, source)
            self._send_alert(text, url, signal, source)
        except Exception as exc:
            logger.error("Presidential post processing error: %s", exc)

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        """Parse an ISO-8601 or RFC-2822 (RSS) timestamp to aware UTC."""
        if not value:
            return None
        # ISO-8601, tolerating a trailing 'Z'.
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass
        # RFC-2822 (RSS pubDate, e.g. "Wed, 11 Jun 2026 14:03:00 GMT").
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    def _is_recent(self, created_at: str) -> bool:
        """True if the post is within PRESIDENTIAL_FRESH_MINUTES.

        Unparseable timestamps are treated as recent — we never silently drop a
        potentially breaking post just because its date format was odd.
        """
        dt = self._parse_dt(created_at)
        if dt is None:
            return True
        cutoff = datetime.now(tz=UTC) - timedelta(
            minutes=settings.PRESIDENTIAL_FRESH_MINUTES
        )
        return dt >= cutoff

    def _classify_post(self, text: str) -> PresidentialSignal | None:
        """Direction from market keywords, longest phrase wins, with negation.

        Returns None when no market keyword is present (so non-market posts make
        no noise). Resolution rules:
          • Longest, most specific phrase wins on overlap — "no deal" beats
            "deal", "tariff reduction" beats "tariff".
          • A SHORT keyword preceded (within 30 chars) by a de-escalation word
            flips bullish — so "I cancelled the scheduled strikes" reads LONG,
            not SHORT (the live miss this whole upgrade exists to fix).
          • Ties break toward the longer matched phrase.
        """
        t = text.lower()
        matches: list[tuple[int, int, str, str]] = []  # (start, end, polarity, kw)
        for kw in settings.PRESIDENTIAL_MARKET_KEYWORDS_LONG:
            start = 0
            needle = kw.lower()
            while (idx := t.find(needle, start)) != -1:
                matches.append((idx, idx + len(needle), "pos", kw))
                start = idx + len(needle)
        for kw in settings.PRESIDENTIAL_MARKET_KEYWORDS_SHORT:
            start = 0
            needle = kw.lower()
            while (idx := t.find(needle, start)) != -1:
                matches.append((idx, idx + len(needle), "neg", kw))
                start = idx + len(needle)

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
                    positive.append(kw)  # de-escalation → bullish
                else:
                    negative.append(kw)
            else:
                positive.append(kw)

        if not positive and not negative:
            return None  # no market keywords — ignore

        if len(positive) > len(negative):
            direction = "long"
        elif len(negative) > len(positive):
            direction = "short"
        else:  # tie — the longer, more specific phrase decides
            longest_pos = max((len(k) for k in positive), default=0)
            longest_neg = max((len(k) for k in negative), default=0)
            direction = "long" if longest_pos >= longest_neg else "short"

        matched = positive if direction == "long" else negative
        return PresidentialSignal(
            direction=direction,
            confidence=settings.PRESIDENTIAL_SHOCK_CONFIDENCE,
            keywords=matched,
            suggested_instruments=self._get_instruments(direction, matched),
        )

    @staticmethod
    def _get_instruments(direction: str, keywords: list[str]) -> list[str]:
        """Suggest affected instruments from the matched keywords."""
        kw_lower = [k.lower() for k in keywords]

        tariff_kw = {"tariff", "trade deal", "tariff reduction", "no tariff", "pause tariffs"}
        iran_kw = {"strike", "ceasefire", "cancelled strikes", "attack", "military action"}

        if any(any(t in k for t in tariff_kw) for k in kw_lower):
            if direction == "long":
                return ["QQQ calls", "SPY calls", "retail ETF (XRT) calls"]
            return ["QQQ puts", "SPY puts", "China ETF (FXI) puts"]

        if any(any(i in k for i in iran_kw) for k in kw_lower):
            if direction == "long":
                return ["QQQ calls", "airline calls (JETS)", "oil puts (USO)"]
            return ["oil calls (USO)", "defense calls (ITA)", "gold calls (GLD)"]

        if direction == "long":
            return ["QQQ calls", "SPY calls"]
        return ["QQQ puts", "SPY puts"]

    # ── Persistence + alerting ────────────────────────────────────────────────
    def _log_to_db(
        self,
        post_id: str,
        text: str,
        url: str,
        signal: PresidentialSignal,
        source: str,
    ) -> None:
        try:
            from data.db import get_connection

            with get_connection() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO presidential_signals
                       (post_id, post_text, post_url, source, direction,
                        confidence, keywords, suggested_instruments, alerted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        post_id,
                        text[:2000],
                        url,
                        source,
                        signal.direction,
                        signal.confidence,
                        json.dumps(signal.keywords),
                        json.dumps(signal.suggested_instruments),
                    ),
                )
        except Exception as exc:
            logger.error("Failed to log presidential signal: %s", exc)

    def _send_alert(
        self,
        text: str,
        url: str,
        signal: PresidentialSignal,
        source: str,
    ) -> None:
        if self.alert_engine is None:
            logger.warning("Presidential alert skipped — no AlertEngine wired")
            return

        snippet = text[:280] + ("..." if len(text) > 280 else "")
        body = (
            f"**Post:** {snippet}\n\n"
            f"**Signal:** {signal.direction.upper()} (confidence {signal.confidence}%)\n"
            f"**Keywords:** {', '.join(signal.keywords)}\n"
            f"**Suggested instruments:** {', '.join(signal.suggested_instruments)}\n\n"
            f"**Source:** {source} (lag: {_SOURCE_LAG.get(source, 'unknown')})\n"
            f"**Link:** {url}\n\n"
            "⚡ ACT WITHIN 2-3 MINUTES for best entry"
        )
        arrow = "📈" if signal.direction == "long" else "📉"
        self.alert_engine.send_alert(
            title=f"🇺🇸 PRESIDENTIAL POST — {arrow} {signal.direction.upper()}",
            body=body,
            channel="alerts",
            color="green" if signal.direction == "long" else "red",
            dedup=False,
        )

    # ── Source chain (runtime fallback) ───────────────────────────────────────
    @staticmethod
    def _has_key(value: object) -> bool:
        """True only for a non-empty, non-whitespace API key."""
        return bool(value) and bool(str(value).strip())

    def _usable_sources(self) -> list[str]:
        """Configured sources in priority order, with RSS always last (terminal)."""
        key_for = {
            "scrapecreators": settings.SCRAPECREATORS_API_KEY,
            "tweetstream": settings.TWEETSTREAM_API_KEY,
            "apify": settings.APIFY_API_KEY,
        }
        out = [
            s for s in settings.PRESIDENTIAL_SOURCE_PRIORITY
            if s == "rss" or (s in key_for and self._has_key(key_for[s]))
        ]
        if "rss" not in out:
            out.append("rss")  # RSS needs no key and always works — terminal fallback
        return out

    def _chain_from(self, start_source: str) -> list[str]:
        """The fallback chain beginning at ``start_source`` (ending at RSS)."""
        chain = self._usable_sources()
        if start_source in chain:
            return chain[chain.index(start_source):]
        return chain

    def _runner_for(self, source: str):
        return {
            "tweetstream": self._run_tweetstream,
            "scrapecreators": self._run_scrapecreators_loop,
            "apify": self._run_apify_loop,
            "rss": self._run_rss_loop,
        }.get(source, self._run_rss_loop)

    # ── Startup catch-up (replay posts missed during downtime) ────────────────
    def _get_last_processed_timestamp(self) -> datetime | None:
        """Most recent ``presidential_signals.created_at`` (None if table empty)."""
        try:
            from data.db import get_connection

            with get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(created_at) FROM presidential_signals"
                ).fetchone()
            value = row[0] if row else None
            return self._parse_dt(str(value)) if value else None
        except Exception as exc:
            logger.debug("Presidential last-processed lookup failed: %s", exc)
            return None

    def _fetch_posts(self, source: str) -> list[dict[str, Any]]:
        """Normalized recent posts for catch-up. TweetStream has no pollable
        history, so it (and any gap) falls back to the RSS feed."""
        try:
            if source == "scrapecreators":
                return [_normalize_post(p) for p in (self._fetch_scrapecreators() or [])]
            if source == "apify":
                return [_normalize_post(p) for p in (self._fetch_apify() or [])]
            return self._fetch_rss_posts()
        except Exception as exc:
            logger.debug("Presidential catch-up fetch (%s) failed: %s", source, exc)
            return self._fetch_rss_posts() if source != "rss" else []

    def _startup_catchup(self, source: str) -> None:
        """Replay posts published since the last processed one (or last 60 min).

        Bridges downtime: a market-moving post during an outage is alerted on
        restart instead of being lost to the 15-minute freshness window.
        """
        try:
            last_seen = self._get_last_processed_timestamp()
            cutoff = last_seen or (
                datetime.now(tz=UTC)
                - timedelta(minutes=settings.PRESIDENTIAL_CATCHUP_MINUTES)
            )
            logger.info(
                "Presidential catch-up from %s (last_seen=%s)",
                cutoff.isoformat(), last_seen.isoformat() if last_seen else "none",
            )
            posts = self._fetch_posts(source)[: settings.PRESIDENTIAL_CATCHUP_MAX_POSTS]
            missed = [
                p for p in posts
                if (dt := self._parse_dt(p.get("created_at", ""))) is not None and dt > cutoff
            ]
            logger.info("Presidential catch-up: %d missed post(s) to process", len(missed))
            for p in missed:
                self._process_post(
                    p["post_id"], p["text"], p["created_at"], p["url"], source,
                    cutoff=cutoff,
                )
        except Exception as exc:
            logger.warning("Presidential catch-up failed: %s", exc)

    def _run_chain(self, start_source: str) -> None:
        """Catch up on missed posts, then run sources in priority order, advancing
        whenever one's loop gives up.

        A loop returns when its source is exhausted (402/auth) or fails repeatedly;
        the next source then starts. RSS is terminal (its loop never returns), so
        the monitor always ends up on RSS rather than spinning on a dead source.
        """
        self._startup_catchup(start_source)
        for source in self._chain_from(start_source):
            if not self._running:
                return
            self.active_source = source
            logger.info("Presidential monitor: source=%s (lag %s)",
                        source, _SOURCE_LAG.get(source, "?"))
            self._runner_for(source)()  # blocks until the loop returns or stops
            if not self._running:
                return
            if source != "rss":
                logger.warning(
                    "Presidential source %s gave up — switching to next in chain", source
                )
        # Safety net: never leave the monitor without a running source.
        if self._running:
            self.active_source = "rss"
            logger.warning("Presidential monitor: all sources exhausted — on RSS")
            self._run_rss_loop()

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Detect the best source and run the fallback chain in a daemon thread."""
        self._running = True
        # Set synchronously so callers see the initial source immediately.
        self.active_source = self.detect_active_source()
        self._thread = threading.Thread(
            target=self._run_chain, args=(self.active_source,),
            daemon=True, name="presidential-monitor",
        )
        self._thread.start()
        logger.info("Presidential monitor started | source=%s", self.active_source)

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception as exc:
                logger.warning("Error closing presidential WebSocket: %s", exc)
