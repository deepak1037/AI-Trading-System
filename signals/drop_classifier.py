"""Drop classifier (Phase 3, Module B, Step 7).

When a stock drops hard (>``DROP_ALERT_THRESHOLD_PCT`` over the lookback window)
this classifies *why*, which decides whether it's a bounce candidate:

    PURE_SENTIMENT  tweet / political / personal noise — no fundamental damage.
                    Highest bounce confidence.
    HYBRID          a real but one-time event (charge, export restriction) the
                    market over-extrapolated. Recovery is thesis-dependent.
    FUNDAMENTAL     real, permanent damage (revenue miss, guidance cut, lost
                    customer). DO NOT trade the bounce.
    EARNINGS_MISS   post-earnings drop — handled separately, outcome varies.

The classifier is deterministic-first (keyword + peer + analyst signals) with an
optional Claude pass that refines the verdict. The LLM is never required — any
failure (no key, API error, bad JSON) falls back to the rule-based verdict, so
the classifier always returns a usable ``DropClassification``.

It is also reused by the position watcher to answer "is this drop in my open
position sentiment or fundamental?" (Phase 3 note 6).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

PURE_SENTIMENT = "PURE_SENTIMENT"
HYBRID = "HYBRID"
FUNDAMENTAL = "FUNDAMENTAL"
EARNINGS_MISS = "EARNINGS_MISS"

# Sector → representative ETF for the peer-comparison check.
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

_SENTIMENT_KEYWORDS = (
    "tweet", "posted", "post ", "says", "said", "claims", "rumor", "rumour",
    "political", "controversy", "personal", "divorce", "arrested",
    "fired ceo", "ceo drama", "government scrutiny", "social media",
    "feud", "spat", "headline risk", "investigation (no charges)",
)

_ONE_TIME_KEYWORDS = (
    "charge", "write-down", "write down", "write-off", "writeoff", "impairment",
    "one-time", "one time", "non-recurring", "nonrecurring", "settlement",
    "export restriction", "export ban", "regulatory fine", "supply chain disruption",
    "temporary", "restriction",
)

_RECURRING_KEYWORDS = (
    "recurring", "ongoing", "permanent ban", "lost contract", "losing customers",
    "competition gaining share", "market share loss", "margin compression",
    "pricing power lost", "structural", "secular decline",
)

# Earnings-report context (distinguishes EARNINGS_MISS from a generic miss).
_EARNINGS_KEYWORDS = (
    "earnings", "quarterly results", "post-earnings", "q1 results", "q2 results",
    "q3 results", "q4 results", "reported results", "earnings call",
)


class DropClassification(BaseModel):
    """Why a stock dropped, and whether it's worth bouncing."""

    ticker: str
    drop_pct: float
    classification: str = HYBRID
    confidence: int = Field(default=0, ge=0, le=100)
    cause_summary: str = ""
    recovery_expected: bool = False
    recovery_timeframe: str = "unknown"      # days | weeks | months | unknown
    bounce_confidence: int = Field(default=0, ge=0, le=100)
    reasoning: str = ""

    fundamental_signals: list[str] = Field(default_factory=list)
    sentiment_signals: list[str] = Field(default_factory=list)
    institutional_action: str = "neutral"    # buying | selling | neutral
    sector_peers: str = "unknown"            # sector_wide | partial_sector | isolated
    model: str = "rule_based"


class DropClassifier:
    """Classifies a significant drop as sentiment / hybrid / fundamental."""

    def __init__(self) -> None:
        self._client: Any = None

    # ── public API ─────────────────────────────────────────────────────────────
    def classify(
        self,
        ticker: str,
        drop_pct: float,
        news_headlines: list[str] | None = None,
        peer_action: str | None = None,
        analyst_action: str | None = None,
        institutional_action: str | None = None,
        use_llm: bool = True,
    ) -> DropClassification:
        """Classify a drop. ``drop_pct`` is positive magnitude (e.g. 7.5 = −7.5%)."""
        headlines = news_headlines or []
        fundamental = self._check_fundamental_signals(headlines, analyst_action)
        sentiment = self._check_sentiment_signals(headlines)
        one_time = self._check_one_time_vs_recurring(headlines)
        is_earnings = self._is_earnings_context(headlines)
        peers = peer_action or self._check_sector_peers(ticker)
        inst = institutional_action or "neutral"

        result = self._rule_based(
            ticker, drop_pct, fundamental, sentiment, one_time, peers, inst, is_earnings
        )

        if use_llm and settings.ANTHROPIC_API_KEY:
            try:
                return self._llm_classify(
                    ticker, drop_pct, headlines, fundamental, sentiment,
                    one_time, peers, analyst_action, result,
                )
            except Exception as exc:  # noqa: BLE001 — keep the rule-based verdict
                logger.error("LLM drop classification failed for %s: %s", ticker, exc)
        return result

    # ── deterministic checks ─────────────────────────────────────────────────────
    @staticmethod
    def _check_fundamental_signals(
        headlines: list[str], analyst_action: str | None = None
    ) -> list[str]:
        """Keyword (and analyst-action) detection of real fundamental damage."""
        text = " ".join(headlines).lower()
        signals: list[str] = []
        if any(k in text for k in ("estimate cut", "estimates cut", "downgrade", "eps cut", "cut estimates")):
            signals.append("Analyst EPS estimates cut")
        if "guidance" in text and any(
            k in text for k in ("cut", "lower", "lowered", "reduce", "slash", "withdraw")
        ):
            signals.append("Company guidance lowered")
        if any(k in text for k in ("lost customer", "lost a major customer", "customer cancellation", "lost contract", "major customer")):
            signals.append("Major customer loss")
        if any(k in text for k in ("revenue miss", "missed revenue", "sales miss", "revenue fell short", "revenue shortfall")):
            signals.append("Revenue miss vs estimates")
        if any(k in text for k in ("new competitor", "competition", "rival", "losing market share", "market share loss")):
            signals.append("Direct competition threat")
        if analyst_action and analyst_action.lower() in ("downgrade", "cut", "negative"):
            signals.append(f"Analyst action: {analyst_action}")
        return signals

    @staticmethod
    def _check_sentiment_signals(headlines: list[str]) -> list[str]:
        """Signs the drop is sentiment-driven, not fundamental."""
        signals: list[str] = []
        for h in headlines:
            hl = h.lower()
            for kw in _SENTIMENT_KEYWORDS:
                if kw.strip() in hl:
                    signals.append(f"Sentiment trigger: {kw.strip()}")
                    break
        return signals

    @staticmethod
    def _check_one_time_vs_recurring(headlines: list[str]) -> dict:
        """Key HYBRID distinction: one-time (recoverable) vs recurring (avoid)."""
        text = " ".join(headlines).lower()
        is_one_time = any(kw in text for kw in _ONE_TIME_KEYWORDS)
        is_recurring = any(kw in text for kw in _RECURRING_KEYWORDS)
        return {"is_one_time": is_one_time, "is_recurring": is_recurring}

    @staticmethod
    def _is_earnings_context(headlines: list[str]) -> bool:
        text = " ".join(headlines).lower()
        return any(kw in text for kw in _EARNINGS_KEYWORDS)

    def _check_sector_peers(self, ticker: str) -> str:
        """sector_wide | partial_sector | isolated, from the sector ETF's move."""
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info
            sector = info.get("sector")
            etf = SECTOR_ETF_MAP.get(sector or "", "SPY")
            hist = yf.Ticker(etf).history(period=f"{settings.DROP_LOOKBACK_DAYS + 1}d")
            if hist is None or hist.empty or len(hist) < 2:
                return "unknown"
            move = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100.0
            return self._peer_bucket(float(move))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Peer comparison failed for %s: %s", ticker, exc)
            return "unknown"

    @staticmethod
    def _peer_bucket(etf_move_pct: float) -> str:
        if etf_move_pct < settings.DROP_SECTOR_WIDE_PCT:
            return "sector_wide"
        if etf_move_pct < settings.DROP_PARTIAL_SECTOR_PCT:
            return "partial_sector"
        return "isolated"

    # ── rule-based verdict ──────────────────────────────────────────────────────
    def _rule_based(
        self,
        ticker: str,
        drop_pct: float,
        fundamental: list[str],
        sentiment: list[str],
        one_time: dict,
        peers: str,
        institutional: str,
        is_earnings: bool = False,
    ) -> DropClassification:
        classification, confidence = self._decide(
            fundamental, sentiment, one_time, is_earnings
        )
        profile = self._recovery_profile(classification)
        cause = self._cause_summary(classification, fundamental, sentiment, one_time)
        bounce = self._bounce_confidence(classification, peers, institutional)

        return DropClassification(
            ticker=ticker,
            drop_pct=drop_pct,
            classification=classification,
            confidence=confidence,
            cause_summary=cause,
            recovery_expected=profile["recovery_expected"],
            recovery_timeframe=profile["timeframe"],
            bounce_confidence=bounce,
            reasoning=self._reasoning(classification, fundamental, sentiment, one_time, peers),
            fundamental_signals=fundamental,
            sentiment_signals=sentiment,
            institutional_action=institutional,
            sector_peers=peers,
            model="rule_based",
        )

    def _decide(
        self,
        fundamental: list[str],
        sentiment: list[str],
        one_time: dict,
        is_earnings: bool = False,
    ) -> tuple[str, int]:
        """Return (classification, confidence) from the deterministic signals."""
        n_fund = len(fundamental)
        has_one_time = one_time["is_one_time"]
        has_recurring = one_time["is_recurring"]

        # Post-earnings drop with no clear permanent damage → EARNINGS_MISS.
        # (A guidance cut / multiple fundamental signals still routes to
        # FUNDAMENTAL below — those are real damage, not just a reaction.)
        if is_earnings and n_fund < settings.DROP_FUNDAMENTAL_MIN_SIGNALS and not has_recurring:
            return EARNINGS_MISS, 65

        # Strong, repeated fundamental damage → FUNDAMENTAL.
        if n_fund >= settings.DROP_FUNDAMENTAL_MIN_SIGNALS and not has_one_time:
            return FUNDAMENTAL, min(60 + 10 * n_fund, 95)
        # Explicitly recurring damage → FUNDAMENTAL regardless of count.
        if has_recurring and not has_one_time:
            return FUNDAMENTAL, 80
        # One-time event the market over-extrapolated → HYBRID.
        if has_one_time and not has_recurring:
            return HYBRID, 75 if n_fund == 0 else 60
        # Pure noise: sentiment triggers, no fundamental damage.
        if sentiment and n_fund == 0 and not has_one_time:
            return PURE_SENTIMENT, min(70 + 5 * len(sentiment), 95)
        # A single fundamental signal, no one-time framing → lean FUNDAMENTAL.
        if n_fund >= 1:
            return FUNDAMENTAL, 55
        # Nothing clear: if any sentiment, treat as sentiment; else ambiguous hybrid.
        if sentiment:
            return PURE_SENTIMENT, 55
        return HYBRID, 40

    @staticmethod
    def _recovery_profile(classification: str) -> dict:
        return {
            PURE_SENTIMENT: {"recovery_expected": True, "timeframe": "days"},
            HYBRID: {"recovery_expected": True, "timeframe": "weeks"},
            FUNDAMENTAL: {"recovery_expected": False, "timeframe": "months"},
            EARNINGS_MISS: {"recovery_expected": True, "timeframe": "weeks"},
        }.get(classification, {"recovery_expected": False, "timeframe": "unknown"})

    @staticmethod
    def _bounce_confidence(classification: str, peers: str, institutional: str) -> int:
        base = {
            PURE_SENTIMENT: 85, HYBRID: 65, FUNDAMENTAL: 10, EARNINGS_MISS: 40,
        }.get(classification, 30)
        if classification == FUNDAMENTAL:
            return base  # never inflate a fundamental-damage bounce
        if peers == "isolated":
            base += 10   # company-specific, cleaner bounce
        elif peers == "sector_wide":
            base -= 15   # macro drag, muddier
        if institutional == "buying":
            base += 10
        elif institutional == "selling":
            base -= 15
        return max(0, min(base, 100))

    @staticmethod
    def _cause_summary(
        classification: str, fundamental: list[str], sentiment: list[str], one_time: dict
    ) -> str:
        if classification == FUNDAMENTAL:
            return "; ".join(fundamental) or "Fundamental damage to the business"
        if classification == HYBRID:
            return "One-time event the market over-extrapolated"
        if classification == PURE_SENTIMENT:
            return "; ".join(sentiment) or "Sentiment-driven selloff, no fundamental damage"
        return "Post-earnings reaction"

    @staticmethod
    def _reasoning(
        classification: str,
        fundamental: list[str],
        sentiment: list[str],
        one_time: dict,
        peers: str,
    ) -> str:
        bits = [f"{len(fundamental)} fundamental signal(s)", f"{len(sentiment)} sentiment signal(s)"]
        if one_time["is_one_time"]:
            bits.append("one-time event detected")
        if one_time["is_recurring"]:
            bits.append("recurring-risk language detected")
        bits.append(f"peers: {peers}")
        return f"Classified {classification} — " + ", ".join(bits) + "."

    # ── LLM refinement ──────────────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
        return self._client

    def _llm_classify(
        self,
        ticker: str,
        drop_pct: float,
        headlines: list[str],
        fundamental: list[str],
        sentiment: list[str],
        one_time: dict,
        peers: str,
        analyst_action: str | None,
        fallback: DropClassification,
    ) -> DropClassification:
        client = self._ensure_client()
        prompt = _CLASSIFICATION_PROMPT.format(
            drop_pct=drop_pct,
            days=settings.DROP_LOOKBACK_DAYS,
            headlines="\n".join(f"- {h}" for h in headlines) or "(none)",
            fundamental_signals=fundamental or "none",
            sentiment_signals=sentiment or "none",
            is_one_time=one_time["is_one_time"],
            peer_action=peers,
            analyst_action=analyst_action or "unknown",
        )
        resp = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            system="You are an expert equity analyst. Respond in JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        data = self._parse_json(text)
        return DropClassification(
            ticker=ticker,
            drop_pct=drop_pct,
            classification=data.get("classification", fallback.classification),
            confidence=int(data.get("confidence", fallback.confidence)),
            cause_summary=data.get("cause_summary", fallback.cause_summary),
            recovery_expected=bool(data.get("recovery_expected", fallback.recovery_expected)),
            recovery_timeframe=data.get("recovery_timeframe", fallback.recovery_timeframe),
            bounce_confidence=int(data.get("bounce_confidence", fallback.bounce_confidence)),
            reasoning=data.get("reasoning", fallback.reasoning),
            fundamental_signals=fundamental,
            sentiment_signals=sentiment,
            institutional_action=fallback.institutional_action,
            sector_peers=peers,
            model=settings.LLM_MODEL,
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("LLM response contained no JSON object")
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else {}


_CLASSIFICATION_PROMPT = """A stock dropped {drop_pct:.1f}% in the last {days} days.

News headlines:
{headlines}

Fundamental signals detected: {fundamental_signals}
Sentiment signals detected: {sentiment_signals}
One-time charge present: {is_one_time}
Sector peers movement: {peer_action}
Analyst revisions: {analyst_action}

Classify this drop:
PURE_SENTIMENT: No fundamental damage, pure noise/fear
HYBRID: Real event but one-time, market overreacted
FUNDAMENTAL: Real permanent damage to business
EARNINGS_MISS: Post-earnings drop

Respond in JSON:
{{
  "classification": "PURE_SENTIMENT|HYBRID|FUNDAMENTAL|EARNINGS_MISS",
  "confidence": 0-100,
  "cause_summary": "brief cause description",
  "recovery_expected": true/false,
  "recovery_timeframe": "days|weeks|months|unknown",
  "bounce_confidence": 0-100,
  "reasoning": "2-3 sentences",
  "do_not_trade_if": "conditions that would change this"
}}"""


__all__ = [
    "DropClassification",
    "DropClassifier",
    "PURE_SENTIMENT",
    "HYBRID",
    "FUNDAMENTAL",
    "EARNINGS_MISS",
    "SECTOR_ETF_MAP",
]
