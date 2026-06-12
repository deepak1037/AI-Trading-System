"""LLM exit-decision engine (Phase 2 Step 6).

Supplements the deterministic ``ExitRulesEngine`` with a Claude assessment for
the cases the rules flag for review (chiefly Bucket 3 at a profit target). The
rules engine remains the source of truth: if the LLM call fails for any reason
(no key, transient API error, unparseable response) this engine falls back to a
rule-derived recommendation so a decision is always produced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from broker_client.buckets.exit_rules import (
    FULL_EXIT,
    HOLD,
    LADDER,
    LLM_REVIEW,
    ROLL,
    STOP_LOSS,
    ExitRulesEngine,
)
from broker_client.buckets.models import BucketPosition
from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)

# Recommendation vocabulary the LLM must use (distinct from the rules' actions).
RECOMMENDATIONS = {"FULL_EXIT", "ROLL_UP", "LADDER", "HOLD"}

# Map rules-engine actions → LLM-style recommendations for the fallback path.
_ACTION_TO_REC = {
    FULL_EXIT: "FULL_EXIT",
    STOP_LOSS: "FULL_EXIT",
    ROLL: "ROLL_UP",
    LADDER: "LADDER",
    LLM_REVIEW: "HOLD",
    HOLD: "HOLD",
}

_SYSTEM_PROMPT = """You are an expert options trader analyzing a position exit \
decision. You follow a strict three-bucket framework:
- Bucket 1 (MSP/Wheel): Always prefer rolling over cutting losses
- Bucket 2B (Defined Risk): Accept losses at stop loss, never roll
- Bucket 3 (LEAP/Event): Binary outcome, exit at profit or expiry

Respond ONLY in JSON format with these exact fields:
{
  "recommendation": "FULL_EXIT | ROLL_UP | LADDER | HOLD",
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation",
  "suggested_action": {
    "description": "What to do exactly",
    "sell": "current position description",
    "buy": "new position if rolling (null if exiting)",
    "net_credit": estimated_credit_if_rolling
  },
  "exit_trigger": "What would change this recommendation",
  "tax_note": "Short term vs long term gain consideration if relevant"
}"""


class SuggestedAction(BaseModel):
    """The concrete action the review recommends."""

    description: str = ""
    sell: str | None = None
    buy: str | None = None
    net_credit: float | None = None


class ExitReview(BaseModel):
    """A full exit review — persisted to the ``exit_reviews`` table."""

    ticker: str
    recommendation: str                # FULL_EXIT | ROLL_UP | LADDER | HOLD
    confidence: int = Field(ge=0, le=100)
    reasoning: str = ""
    suggested_action: SuggestedAction = Field(default_factory=SuggestedAction)
    exit_trigger: str = ""
    tax_note: str = ""

    profit_pct: float = 0.0
    dte_remaining: int = 0
    thesis_status: str = "active"
    macro_regime: str = ""
    source: str = "llm"                 # "llm" | "rule_fallback"
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class LLMExitEngine:
    """Claude-assisted exit reviews with a deterministic rule fallback."""

    def __init__(
        self,
        exit_rules: ExitRulesEngine | None = None,
        db_path: str | None = None,
    ) -> None:
        self._rules = exit_rules or ExitRulesEngine()
        self._db_path = db_path or settings.DB_PATH
        self._client: Any = None  # lazy anthropic.Anthropic

    # ── public API ───────────────────────────────────────────────────────────
    def evaluate_exit(
        self,
        position: BucketPosition,
        market_context: dict | None = None,
    ) -> ExitReview:
        """Return an exit review for ``position``.

        Tries the LLM first; on any failure degrades to a rule-based review so
        callers always get an actionable recommendation.
        """
        context = self.build_context(position, market_context or {})
        try:
            review = self._call_llm(position, context)
            logger.info(
                "LLMExitEngine: %s → %s (conf=%d) [llm]",
                position.ticker, review.recommendation, review.confidence,
            )
            return review
        except Exception as exc:
            logger.warning(
                "LLMExitEngine: LLM review failed for %s (%s) — using rule fallback",
                position.ticker, exc,
            )
            return self.rule_based_review(position, context)

    def build_context(self, p: BucketPosition, extra: dict) -> dict:
        """Assemble the context dict handed to the LLM (and stored for audit)."""
        context: dict[str, Any] = {
            "ticker": p.ticker,
            "strategy": p.sub_type,
            "bucket": p.bucket,
            "recoverable": p.recoverable,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "profit_pct": p.profit_pct,
            "dte_remaining": p.dte_remaining,
            "dte_original": p.original_dte,
            "dte_used_pct": round(p.dte_used_pct, 1),
            "delta": p.delta,
            "entry_thesis": p.entry_thesis,
            "thesis_status": p.thesis_status,
            "thesis_completion_pct": p.thesis_completion_pct,
            "iv_rank_current": p.iv_rank,
            "iv_at_entry": p.iv_at_entry,
            "days_held": p.days_held,
        }
        # Caller-supplied market/portfolio context (macro regime, opportunities…).
        context.update(extra)
        return context

    def rule_based_review(self, p: BucketPosition, context: dict | None = None) -> ExitReview:
        """Build an ExitReview purely from the deterministic rules engine."""
        rec = self._rules.check_exit(p)
        action = rec.action if rec else HOLD
        recommendation = _ACTION_TO_REC.get(action, "HOLD")
        reason = rec.reason if rec else "No exit rule triggered"
        return ExitReview(
            ticker=p.ticker,
            recommendation=recommendation,
            confidence=60,
            reasoning=f"Rule-based fallback ({action}): {reason}",
            suggested_action=SuggestedAction(description=reason),
            exit_trigger="A change in DTE, profit, or thesis status",
            profit_pct=p.profit_pct,
            dte_remaining=p.dte_remaining,
            thesis_status=p.thesis_status,
            macro_regime=str((context or {}).get("macro_regime", "")),
            source="rule_fallback",
        )

    # ── persistence ──────────────────────────────────────────────────────────
    def persist_review(self, review: ExitReview, position_id: int | None = None) -> int:
        """Write a review to the ``exit_reviews`` table; returns the row id."""
        import sqlite3

        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO exit_reviews
                   (position_id, ticker, review_date, profit_pct, dte_remaining,
                    thesis_status, macro_regime, recommendation, confidence,
                    reasoning, suggested_action, executed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    position_id,
                    review.ticker,
                    review.reviewed_at.isoformat(),
                    review.profit_pct,
                    review.dte_remaining,
                    review.thesis_status,
                    review.macro_regime,
                    review.recommendation,
                    review.confidence,
                    review.reasoning,
                    review.suggested_action.model_dump_json(),
                ),
            )
            return int(cur.lastrowid or 0)

    # ── Discord formatting ───────────────────────────────────────────────────
    @staticmethod
    def format_discord(review: ExitReview) -> str:
        """Human-readable review body (CLAUDE_PHASE2 §6 format)."""
        sa = review.suggested_action
        return (
            f"Current P&L: {review.profit_pct:+.1f}%\n"
            f"DTE remaining: {review.dte_remaining}\n"
            f"Thesis: {review.thesis_status}\n\n"
            f"🤖 LLM RECOMMENDATION: {review.recommendation} "
            f"(confidence {review.confidence}%)\n"
            f'"{review.reasoning}"\n\n'
            f"Options:\n"
            f"  A) FULL EXIT    → Bank {review.profit_pct:+.1f}% profit\n"
            f"  B) ROLL UP      → {sa.description or 'roll to a higher strike/expiry'}\n"
            f"  C) SCALE OUT    → Close 50%, let 50% ride\n"
            f"  D) HOLD         → {review.exit_trigger}"
        )

    def send_review_alert(self, review: ExitReview, alert_engine: Any) -> bool:
        """Send the review to Discord #opportunities via AlertEngine."""
        if alert_engine is None:
            return False
        return bool(alert_engine.send_alert(
            title=f"💡 EXIT REVIEW: {review.ticker} {review.recommendation}",
            body=self.format_discord(review),
            channel="opportunities",
            color="green",
        ))

    # ── LLM plumbing ─────────────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not settings.ANTHROPIC_API_KEY:
            raise DataError("ANTHROPIC_API_KEY not set — LLM exit review unavailable")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise DataError("anthropic package not installed") from exc
        self._client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        return self._client

    def _build_user_prompt(self, p: BucketPosition, context: dict) -> str:
        return (
            "Analyze this position and recommend an exit action:\n\n"
            f"{json.dumps(context, indent=2, default=str)}\n\n"
            "Consider:\n"
            "1. Is the thesis still intact or complete?\n"
            "2. Is rolling beneficial given DTE and credit available?\n"
            "3. Are there better opportunities for this capital?\n"
            "4. What does the macro regime suggest?\n"
            f"5. Tax implications (held {p.days_held} days)?"
        )

    def _call_llm(self, p: BucketPosition, context: dict) -> ExitReview:
        client = self._ensure_client()
        user_prompt = self._build_user_prompt(p, context)
        resp = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        )
        return self._parse_review(text, p, context)

    @staticmethod
    def _parse_review(text: str, p: BucketPosition, context: dict) -> ExitReview:
        """Extract and validate the JSON review object from a model response."""
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise DataError("LLM exit review contained no JSON object", response=text[:200])
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise DataError(f"Could not parse exit-review JSON: {exc}") from exc

        rec = str(data.get("recommendation", "HOLD")).upper()
        if rec not in RECOMMENDATIONS:
            rec = "HOLD"
        sa_raw = data.get("suggested_action") or {}
        suggested = SuggestedAction(
            description=str(sa_raw.get("description", "")),
            sell=sa_raw.get("sell"),
            buy=sa_raw.get("buy"),
            net_credit=_as_float(sa_raw.get("net_credit")),
        )
        return ExitReview(
            ticker=p.ticker,
            recommendation=rec,
            confidence=int(max(0, min(100, int(data.get("confidence", 50))))),
            reasoning=str(data.get("reasoning", "")),
            suggested_action=suggested,
            exit_trigger=str(data.get("exit_trigger", "")),
            tax_note=str(data.get("tax_note", "")),
            profit_pct=p.profit_pct,
            dte_remaining=p.dte_remaining,
            thesis_status=p.thesis_status,
            macro_regime=str(context.get("macro_regime", "")),
            source="llm",
        )


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["ExitReview", "SuggestedAction", "LLMExitEngine", "RECOMMENDATIONS"]
