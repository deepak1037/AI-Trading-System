"""Earnings LLM assessor (Phase 3, Step 4).

Asks Claude to assess an earnings trade (IV crush / IV spike) given the IV
analysis and the concrete trade, returning an ``EarningsAssessment``. Mirrors
``LLMROIAnalyzer``'s discipline: lazy Anthropic client, transient-only retries,
strict JSON parsing — and, crucially, a deterministic **rule-based fallback** so
the scanner still produces an assessment when the API key is missing, the model
errors, or the response can't be parsed.
"""

from __future__ import annotations

import json
from typing import Any

from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    EarningsAssessment,
    IVAnalysis,
    IVCrushTrade,
    IVSpikeTrade,
)
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = """You are an expert options trader specializing in earnings \
plays. You analyze IV patterns, historical breach rates, and fundamental context \
to assess the risk/reward of earnings trades. Always respond in JSON only."""

_USER_PROMPT = """Analyze this earnings trade setup:

Ticker: {ticker}
Earnings: {earnings_date} ({earnings_time})
Strategy: {strategy}

IV Analysis:
- Current IV Rank: {iv_rank}/100
- Avg Historical IV Crush: {avg_iv_crush:.1f}%
- IV Crush Consistency: {consistency:.0%} of quarters
- Total Breach Rate: {breach_rate:.0%} (stock exceeds expected move, either direction)
- DOWNSIDE Breach Rate: {downside_breach:.0%} ({downside_breaches} of {quarters} quarters \
breached to the downside) — this is what threatens a short PUT
- Expected Move this quarter: ±{expected_move:.1f}%
- Historical avg actual move: {avg_actual_move:.1f}%

Trade Setup:
{trade_details}

Fundamental context:
- Forecast Revenue YoY: {revenue_yoy}
- Forecast EPS YoY: {eps_yoy}

SIZING GUIDANCE:
- Downside breach 20-30%: this is an elevated-but-acceptable assignment risk —
  recommend EXECUTE at "half" size, and call out the specific downside breach
  history (e.g. "{downside_breaches} of {quarters} quarters breached to the
  downside") in your reasoning. Prefer the more conservative (primary) put strike.
- Downside breach <20%: full size is reasonable if the other metrics agree.
- Downside breach >30%: lean SKIP or "quarter".

Respond with JSON:
{{
  "recommendation": "EXECUTE" | "SKIP" | "REDUCE_SIZE",
  "confidence": 0-100,
  "probability_of_profit": 0-100,
  "key_risks": ["risk1", "risk2"],
  "key_strengths": ["strength1", "strength2"],
  "sizing_suggestion": "full" | "half" | "quarter",
  "reasoning": "2-3 sentence assessment",
  "exit_plan": "specific exit instructions",
  "watch_for": "what would change this assessment"
}}"""


class LLMEarningsAssessor:
    """Claude-backed earnings assessment with a rule-based fallback."""

    def __init__(self) -> None:
        self._client: Any = None

    # ── public API ─────────────────────────────────────────────────────────────
    def assess(
        self,
        ticker: str,
        analysis: IVAnalysis,
        trade: IVCrushTrade | IVSpikeTrade | None,
        event: Any = None,
    ) -> EarningsAssessment:
        """Return Claude's assessment, or the rule-based fallback on any failure."""
        if trade is None:
            return self._rule_based(analysis, trade)
        if not settings.ANTHROPIC_API_KEY:
            logger.debug("No ANTHROPIC_API_KEY — using rule-based earnings assessment")
            return self._rule_based(analysis, trade)
        try:
            return self._call_llm(ticker, analysis, trade, event)
        except Exception as exc:  # noqa: BLE001 — degrade to deterministic rules
            logger.error("LLM earnings assessment failed: %s", exc)
            return self._rule_based(analysis, trade)

    # ── LLM path ───────────────────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
        return self._client

    def _call_llm(
        self,
        ticker: str,
        analysis: IVAnalysis,
        trade: IVCrushTrade | IVSpikeTrade,
        event: Any,
    ) -> EarningsAssessment:
        client = self._ensure_client()
        prompt = self._build_prompt(ticker, analysis, trade, event)
        resp = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        )
        assessment = self._parse(text)
        assessment.model = settings.LLM_MODEL
        return assessment

    def _build_prompt(
        self,
        ticker: str,
        analysis: IVAnalysis,
        trade: IVCrushTrade | IVSpikeTrade,
        event: Any,
    ) -> str:
        quarters = analysis.quarters_analyzed
        downside_breaches = int(round(analysis.breach_rate_lower * quarters)) if quarters else 0
        return _USER_PROMPT.format(
            ticker=ticker,
            earnings_date=trade.earnings_date,
            earnings_time=getattr(trade, "earnings_time", "AMC"),
            strategy=analysis.strategy,
            iv_rank=analysis.iv_rank_current,
            avg_iv_crush=analysis.avg_iv_crush,
            consistency=analysis.iv_crush_consistency,
            breach_rate=analysis.breach_rate,
            downside_breach=analysis.breach_rate_lower,
            downside_breaches=downside_breaches,
            quarters=quarters or "?",
            expected_move=analysis.expected_move_current,
            avg_actual_move=analysis.avg_actual_move,
            trade_details=self._trade_details(trade),
            revenue_yoy=getattr(event, "forecast_revenue_yoy", None),
            eps_yoy=getattr(event, "forecast_eps_yoy", None),
        )

    @staticmethod
    def _trade_details(trade: IVCrushTrade | IVSpikeTrade) -> str:
        if isinstance(trade, IVCrushTrade):
            return (
                f"IV CRUSH (sell premium): sell {trade.put_strike} put @ "
                f"${trade.put_premium:.2f}, expiry {trade.put_expiry}, "
                f"margin ${trade.margin_required:.0f} ({trade.margin_basis}), "
                f"ROI {trade.roi_margin:.1%}, break-even needs {trade.break_even_pct:.1f}% drop."
            )
        return (
            f"IV SPIKE (buy premium): buy {trade.instrument} "
            f"{trade.call_strike}C/{trade.put_strike}P, cost "
            f"${trade.total_premium_paid:.2f}, ENTER {trade.entry_date}, "
            f"EXIT {trade.exit_date} (1 day BEFORE earnings — never hold through)."
        )

    @staticmethod
    def _parse(text: str) -> EarningsAssessment:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("LLM response contained no JSON object")
        data = json.loads(raw[start : end + 1])
        return EarningsAssessment(**data)

    # ── rule-based fallback ──────────────────────────────────────────────────────
    def _rule_based(
        self,
        analysis: IVAnalysis,
        trade: IVCrushTrade | IVSpikeTrade | None,
    ) -> EarningsAssessment:
        """Deterministic assessment from the IV analysis + trade economics."""
        if trade is None:
            return EarningsAssessment(
                recommendation="SKIP", confidence=0, probability_of_profit=0,
                reasoning="No actionable trade for this earnings setup.",
                model="rule_based",
            )

        confidence = analysis.confidence
        if analysis.strategy == IV_CRUSH:
            return self._rule_crush(analysis, trade, confidence)
        if analysis.strategy == IV_SPIKE:
            return self._rule_spike(analysis, trade, confidence)
        return EarningsAssessment(
            recommendation="SKIP", confidence=0,
            reasoning="Strategy is SKIP.", model="rule_based",
        )

    @staticmethod
    def _rec_from_confidence(confidence: int) -> tuple[str, str]:
        if confidence >= settings.CONFIDENCE_CRITICAL:
            return "EXECUTE", "full"
        if confidence >= settings.CONFIDENCE_HIGH:
            return "EXECUTE", "half"
        if confidence >= 50:
            return "REDUCE_SIZE", "quarter"
        return "SKIP", "quarter"

    def _rule_crush(
        self, analysis: IVAnalysis, trade: Any, confidence: int
    ) -> EarningsAssessment:
        # The analyzer already approved this as a valid crush setup → EXECUTE; the
        # confidence drives SIZE (full/half/quarter), not whether to trade.
        pop = int(round((1.0 - analysis.breach_rate_lower) * 100))
        quarters = analysis.quarters_analyzed
        downside_breaches = int(round(analysis.breach_rate_lower * quarters)) if quarters else 0
        rec = "EXECUTE"
        breach_note = ""
        if 0.20 <= analysis.breach_rate_lower <= settings.EARNINGS_MAX_BREACH_RATE and quarters:
            # Elevated-but-acceptable downside breach (20-30%) → HALF size, called out.
            sizing = "half"
            breach_note = (
                f" {downside_breaches} of {quarters} quarters breached to the downside "
                f"({analysis.breach_rate_lower:.0%}) — size HALF and keep the strike conservative."
            )
        elif confidence >= settings.CONFIDENCE_HIGH:
            sizing = "full"
        else:
            sizing = "half"
        strengths = [
            f"Avg IV crush {analysis.avg_iv_crush:.0f}% over "
            f"{analysis.iv_crush_consistency:.0%} of quarters",
            f"Downside breach only {analysis.breach_rate_lower:.0%}",
        ]
        risks = [
            f"Gap below ${trade.put_strike:.0f} put on a surprise miss",
            f"{downside_breaches}/{quarters} quarters broke the expected move to the downside",
        ]
        return EarningsAssessment(
            recommendation=rec, confidence=confidence, probability_of_profit=pop,
            key_strengths=strengths, key_risks=risks, sizing_suggestion=sizing,
            reasoning=(
                f"Crusher with a {trade.break_even_pct:.0f}% downside cushion and "
                f"{trade.roi_margin:.0%} margin ROI; sell the put and let IV collapse."
                + breach_note
            ),
            exit_plan="Close at 50% premium decay or the day after earnings (IV crush done).",
            watch_for="A guidance cut or sector-wide selloff widening the actual move.",
            model="rule_based",
        )

    def _rule_spike(
        self, analysis: IVAnalysis, trade: Any, confidence: int
    ) -> EarningsAssessment:
        rec, sizing = self._rec_from_confidence(confidence)
        pop = int(round(min(analysis.breach_rate, 0.9) * 100))
        return EarningsAssessment(
            recommendation=rec, confidence=confidence, probability_of_profit=pop,
            key_strengths=[
                f"IV low (rank {analysis.iv_rank_current}) with room to expand",
                f"Stock moves big on earnings (avg {analysis.avg_actual_move:.0f}%)",
            ],
            key_risks=[
                "IV may not expand if the event is well-telegraphed",
                "Theta decay while waiting for IV to rise",
            ],
            sizing_suggestion=sizing,
            reasoning=(
                "Buy the straddle into rising IV and capture expansion, NOT the move. "
                f"Hard exit {trade.exit_date} — one day before earnings."
            ),
            exit_plan=(
                f"Exit on {trade.exit_date} (before earnings) or at "
                f"+{trade.target_profit_pct:.0f}% — never hold through the report."
            ),
            watch_for="IV failing to rise into the event (thesis broken — cut early).",
            model="rule_based",
        )


__all__ = ["LLMEarningsAssessor"]
