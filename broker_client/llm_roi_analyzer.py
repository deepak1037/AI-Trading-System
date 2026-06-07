"""LLM-augmented put-selling ROI analyzer.

Combines a quantitative cash-secured / naked put ROI calculation
(``PutROIResult``) with a Claude (Anthropic) assessment that estimates the
*probability-adjusted* return, expected premium decay, and a STRONG_SELL ..
AVOID recommendation (``LLMROIAnalysis``).

The static ROI a screener computes ("5.7% monthly") assumes you hold the put to
expiry. In reality theta decay is front-loaded and most short puts can be closed
at 50% profit in a fraction of the time — so the *realized* annualized ROI is
usually much higher than the static number. The LLM's job is to reason about
that, plus the bounce/assignment scenarios, given the quantitative context we
gather.

Usage::

    analyzer = LLMROIAnalyzer(broker=get_broker())
    analysis = analyzer.analyze("HOOD", strike=8.0, expiry="2026-07-18")
    print(analysis.assessment.recommendation)   # "STRONG_SELL"

Context gathering is best-effort: every external lookup is wrapped so a single
failing data source (no short-interest, sentiment model not downloaded, …)
degrades that one field to "unknown" rather than killing the whole analysis.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from broker_core.base_broker import BaseBroker, OptionsContract
from broker_client.options_engine import OptionsEngine
from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)

Recommendation = Literal["STRONG_SELL", "SELL", "NEUTRAL", "AVOID"]


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────
class PutROIResult(BaseModel):
    """Quantitative ROI for selling one put contract.

    All ``*_per_contract`` figures are per single contract (multiplier shares).
    ROI percentages are ``premium / margin`` scaled to the stated horizon.
    """

    ticker: str
    strike: float
    expiry: str                       # YYYY-MM-DD
    days_to_expiry: int
    underlying_price: float

    bid: float
    ask: float
    mid: float                        # per share
    premium_per_contract: float       # mid * multiplier
    margin_per_contract: float        # capital tied up per contract
    margin_basis: str                 # reg_t | cash_secured

    breakeven: float                  # strike - mid
    otm_pct: float                    # % the strike sits below spot (negative = ITM)

    static_roi_pct: float             # premium / margin over the holding period
    monthly_roi_pct: float            # static scaled to 30 days
    annualized_roi_pct: float         # static scaled to 365 days


class LLMAssessment(BaseModel):
    """Claude's probability-adjusted assessment (parsed from JSON response)."""

    expiry_worthless_probability: int = Field(ge=0, le=100)
    expected_50pct_decay_days: int = Field(ge=0)
    adjusted_monthly_roi_pct: float
    key_risks: list[str] = Field(default_factory=list)
    bounce_scenario: str = ""
    assignment_scenario: str = ""
    recommendation: Recommendation
    confidence: int = Field(ge=0, le=100)
    reasoning: str = ""


class LLMROIAnalysis(BaseModel):
    """Full analysis: quantitative ROI + LLM assessment + the context used."""

    roi: PutROIResult
    assessment: LLMAssessment
    context: dict = Field(default_factory=dict)
    model: str = ""
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are an expert options trader analyzing put selling \
opportunities. Given quantitative data about a stock and its options, assess:
1. Probability the put expires worthless (0-100%)
2. Expected days to reach 50% premium decay
3. Key risk factors that could cause assignment
4. Adjusted monthly ROI given your probability assessment
5. Recommendation: STRONG_SELL | SELL | NEUTRAL | AVOID

Be concise. Output JSON only, matching exactly this schema:
{
  "expiry_worthless_probability": <int 0-100>,
  "expected_50pct_decay_days": <int>,
  "adjusted_monthly_roi_pct": <float>,
  "key_risks": [<string>, ...],
  "bounce_scenario": <string>,
  "assignment_scenario": <string>,
  "recommendation": "STRONG_SELL" | "SELL" | "NEUTRAL" | "AVOID",
  "confidence": <int 0-100>,
  "reasoning": <string, 2-3 sentences>
}"""


class LLMROIAnalyzer:
    """Compute put ROI and enrich it with a Claude assessment."""

    def __init__(self, broker: Optional[BaseBroker] = None) -> None:
        self._broker = broker
        self._options = OptionsEngine(broker=broker)
        self._multiplier = settings.OPTIONS_CONTRACT_MULTIPLIER
        self._client: Any = None  # lazy anthropic.Anthropic — built on first LLM call

    # ── public API ───────────────────────────────────────────────────────────
    def analyze(
        self, ticker: str, strike: float, expiry: str
    ) -> LLMROIAnalysis:
        """End-to-end: build the ROI result then run the LLM assessment."""
        roi = self.build_put_roi(ticker, strike, expiry)
        return self.analyze_put_opportunity(roi, ticker)

    def build_put_roi(
        self,
        ticker: str,
        strike: float,
        expiry: str,
        contract: Optional[OptionsContract] = None,
        underlying_price: Optional[float] = None,
    ) -> PutROIResult:
        """Locate the put contract and compute its quantitative ROI."""
        ticker = ticker.upper().strip()
        if contract is None:
            contract = self._find_put(ticker, strike, expiry)
        if underlying_price is None:
            underlying_price = self._current_price(ticker)

        mid = self._options.mid_price(contract)
        if mid <= 0:
            raise DataError(
                "Put has no usable price (bid/ask/last all zero)",
                ticker=ticker,
                strike=strike,
                expiry=expiry,
            )

        premium_per_contract = mid * self._multiplier
        margin_per_contract = self._margin_per_contract(
            strike, underlying_price, mid
        )
        days = self._days_to_expiry(expiry)
        static_roi = (
            (premium_per_contract / margin_per_contract * 100.0)
            if margin_per_contract > 0
            else 0.0
        )
        # Scale the period return to 30d / 365d horizons.
        scale_days = max(days, 1)
        monthly_roi = static_roi * (30.0 / scale_days)
        annual_roi = static_roi * (365.0 / scale_days)

        otm_pct = (
            (underlying_price - strike) / underlying_price * 100.0
            if underlying_price > 0
            else 0.0
        )

        result = PutROIResult(
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            days_to_expiry=days,
            underlying_price=round(underlying_price, 2),
            bid=round(contract.bid, 2),
            ask=round(contract.ask, 2),
            mid=round(mid, 2),
            premium_per_contract=round(premium_per_contract, 2),
            margin_per_contract=round(margin_per_contract, 2),
            margin_basis=settings.OPTIONS_MARGIN_BASIS,
            breakeven=round(strike - mid, 2),
            otm_pct=round(otm_pct, 2),
            static_roi_pct=round(static_roi, 2),
            monthly_roi_pct=round(monthly_roi, 2),
            annualized_roi_pct=round(annual_roi, 2),
        )
        logger.info(
            "PutROI %s $%s %s: premium=$%.0f margin=$%.0f static=%.1f%% monthly=%.1f%%",
            ticker, strike, expiry, premium_per_contract, margin_per_contract,
            static_roi, monthly_roi,
        )
        return result

    def analyze_put_opportunity(
        self, roi_result: PutROIResult, ticker: str
    ) -> LLMROIAnalysis:
        """Gather context, ask Claude, and return the combined analysis."""
        ticker = ticker.upper().strip()
        context = self._gather_context(ticker, roi_result)
        assessment = self._call_llm(roi_result, ticker, context)
        return LLMROIAnalysis(
            roi=roi_result,
            assessment=assessment,
            context=context,
            model=settings.LLM_MODEL,
        )

    # ── ROI helpers ──────────────────────────────────────────────────────────
    def _find_put(
        self, ticker: str, strike: float, expiry: str
    ) -> OptionsContract:
        chain = self._options.fetch_chain(ticker, expiry=expiry)
        candidates = [
            p for p in chain.puts if abs(p.strike - strike) < 1e-6
        ]
        if not candidates:
            # Fall back to the closest strike and warn.
            if not chain.puts:
                raise DataError(
                    "No puts in chain", ticker=ticker, expiry=expiry
                )
            closest = min(chain.puts, key=lambda p: abs(p.strike - strike))
            logger.warning(
                "No exact %s put at strike %.2f for %s; using closest strike %.2f",
                ticker, strike, expiry, closest.strike,
            )
            return closest
        return candidates[0]

    def _current_price(self, ticker: str) -> float:
        """Best-effort spot price: broker quote first, then yfinance."""
        if self._broker is not None:
            try:
                q = self._broker.get_quote(ticker)
                price = q.last or (q.bid + q.ask) / 2 if (q.bid and q.ask) else q.last
                if price and price > 0:
                    return float(price)
            except Exception as exc:  # noqa: BLE001 — degrade to yfinance
                logger.debug("Broker quote failed for %s: %s", ticker, exc)
        try:
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.debug("yfinance price failed for %s: %s", ticker, exc)
        raise DataError("Could not determine underlying price", ticker=ticker)

    def _margin_per_contract(
        self, strike: float, underlying: float, premium_per_share: float
    ) -> float:
        """Capital tied up per short put contract.

        ``cash_secured``: strike * multiplier (full cash collateral).
        ``reg_t``: standard naked-put initial margin —
            (max(0.20*underlying - OTM_amount, 0.10*strike) + premium) * multiplier
        """
        mult = self._multiplier
        if settings.OPTIONS_MARGIN_BASIS == "cash_secured":
            # Net of premium received, as most brokers hold (strike - premium).
            return max(strike - premium_per_share, 0.0) * mult
        otm_amount = max(underlying - strike, 0.0)
        req = max(0.20 * underlying - otm_amount, 0.10 * strike)
        return (req + premium_per_share) * mult

    @staticmethod
    def _days_to_expiry(expiry: str) -> int:
        try:
            exp = date.fromisoformat(expiry)
        except ValueError:
            exp = datetime.strptime(expiry, "%Y-%m-%d").date()
        return max((exp - datetime.now(tz=timezone.utc).date()).days, 0)

    # ── context gathering (all best-effort) ──────────────────────────────────
    def _gather_context(
        self, ticker: str, roi: PutROIResult
    ) -> dict:
        """Collect every quantitative signal the LLM prompt asks for.

        Each lookup is independently guarded so one failure leaves that field
        as ``None``/"unknown" instead of aborting the analysis.
        """
        ctx: dict = {
            "current_price": roi.underlying_price,
            "high_52w": None,
            "low_52w": None,
            "pct_from_high": None,
            "pct_from_low": None,
            "rsi": None,
            "macd_hist": None,
            "iv": None,        # filled from the option chain if available
            "hv_30": None,
            "volume_ratio": None,
            "sentiment_score": None,
            "short_pct_float": None,
            "short_ratio_days": None,
            "has_earnings_within_30d": None,
            "next_earnings_date": None,
            "institutional_accumulation": None,
            "price_narrative": "",
        }

        hist = self._price_history(ticker)
        self._fill_price_context(ctx, hist, roi.underlying_price)
        self._fill_technicals(ctx, hist, ticker)
        self._fill_iv(ctx, ticker, roi)
        self._fill_sentiment(ctx, ticker)
        self._fill_short_interest(ctx, ticker)
        self._fill_earnings(ctx, ticker)
        self._fill_accumulation(ctx, ticker)
        ctx["price_narrative"] = self._build_narrative(ctx)
        return ctx

    @staticmethod
    def _price_history(ticker: str):
        try:
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period="1y")
            return hist if hist is not None and not hist.empty else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Price history failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _fill_price_context(ctx: dict, hist, current: float) -> None:
        if hist is None:
            return
        try:
            high = float(hist["High"].max())
            low = float(hist["Low"].min())
            ctx["high_52w"] = round(high, 2)
            ctx["low_52w"] = round(low, 2)
            if high > 0:
                ctx["pct_from_high"] = round((current - high) / high * 100.0, 1)
            if low > 0:
                ctx["pct_from_low"] = round((current - low) / low * 100.0, 1)
            # Volume vs 30-day average
            vol = hist["Volume"]
            if len(vol) >= 30:
                avg30 = float(vol.tail(30).mean())
                last_vol = float(vol.iloc[-1])
                if avg30 > 0:
                    ctx["volume_ratio"] = round(last_vol / avg30, 2)
            # 30-day annualized realized (historical) volatility
            closes = hist["Close"].astype(float)
            if len(closes) >= 31:
                rets = closes.pct_change().dropna().tail(30)
                hv = float(rets.std()) * (252 ** 0.5)
                ctx["hv_30"] = round(hv * 100.0, 1)  # as a %
        except Exception as exc:  # noqa: BLE001
            logger.debug("Price-context computation failed: %s", exc)

    def _fill_technicals(self, ctx: dict, hist, ticker: str) -> None:
        if hist is None:
            return
        try:
            import pandas as pd  # noqa: F401  (ensure pandas present)
            from signals.technical_module import TechnicalModule

            df = hist.rename(
                columns={
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                }
            )
            sig = TechnicalModule().score(df, ticker=ticker)
            ctx["rsi"] = sig.metadata.get("rsi")
            ctx["macd_hist"] = sig.metadata.get("macd_hist")
            ctx["technical_direction"] = sig.direction
        except Exception as exc:  # noqa: BLE001
            logger.debug("Technicals failed for %s: %s", ticker, exc)

    @staticmethod
    def _fill_iv(ctx: dict, ticker: str, roi: PutROIResult) -> None:
        """Pull the contract's implied vol; compute a rough IV-vs-HV rank.

        We do not have a 52-week IV history feed, so a true IV *rank* is not
        available. We report the contract IV and, when historical volatility is
        known, an honest IV/HV ratio (>1 = options richer than realized).
        """
        try:
            import yfinance as yf

            opt = yf.Ticker(ticker)
            chain = opt.option_chain(roi.expiry) if roi.expiry else None
            iv = None
            if chain is not None:
                puts = chain.puts
                row = puts[abs(puts["strike"] - roi.strike) < 1e-6]
                if not row.empty and "impliedVolatility" in row:
                    iv = float(row["impliedVolatility"].iloc[0]) * 100.0
            if iv is not None:
                ctx["iv"] = round(iv, 1)
                hv = ctx.get("hv_30")
                if hv:
                    ctx["iv_vs_hv"] = round(iv / hv, 2)
        except Exception as exc:  # noqa: BLE001
            logger.debug("IV fetch failed for %s: %s", ticker, exc)

    @staticmethod
    def _fill_sentiment(ctx: dict, ticker: str) -> None:
        try:
            from signals.sentiment_scorer import SentimentScorer

            sig = SentimentScorer().score(query=ticker)
            ctx["sentiment_score"] = sig.metadata.get("avg_score")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Sentiment failed for %s: %s", ticker, exc)

    @staticmethod
    def _fill_short_interest(ctx: dict, ticker: str) -> None:
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info
            spf = info.get("shortPercentOfFloat")
            if spf is not None:
                ctx["short_pct_float"] = round(float(spf) * 100.0, 1)
            sr = info.get("shortRatio")
            if sr is not None:
                ctx["short_ratio_days"] = round(float(sr), 1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Short interest failed for %s: %s", ticker, exc)

    @staticmethod
    def _fill_earnings(ctx: dict, ticker: str) -> None:
        try:
            import yfinance as yf

            cal = yf.Ticker(ticker).calendar
            ed = None
            if isinstance(cal, dict):
                vals = cal.get("Earnings Date")
                if isinstance(vals, list) and vals:
                    ed = vals[0]
                elif vals is not None:
                    ed = vals
            if ed is not None:
                ed_date = ed.date() if hasattr(ed, "date") else ed
                ctx["next_earnings_date"] = str(ed_date)
                try:
                    days = (date.fromisoformat(str(ed_date)) - datetime.now(tz=timezone.utc).date()).days
                    ctx["has_earnings_within_30d"] = 0 <= days <= 30
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("Earnings date failed for %s: %s", ticker, exc)

    @staticmethod
    def _fill_accumulation(ctx: dict, ticker: str) -> None:
        try:
            from scanner.accumulation_screen import AccumulationScreen

            ctx["institutional_accumulation"] = bool(
                AccumulationScreen()._passes_accumulation(ticker)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Accumulation signal failed for %s: %s", ticker, exc)

    @staticmethod
    def _build_narrative(ctx: dict) -> str:
        parts: list[str] = []
        if ctx.get("pct_from_high") is not None:
            parts.append(f"{abs(ctx['pct_from_high']):.0f}% off 52w high")
        if ctx.get("pct_from_low") is not None:
            parts.append(f"{ctx['pct_from_low']:.0f}% above 52w low")
        if ctx.get("rsi") is not None:
            rsi = ctx["rsi"]
            tag = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
            parts.append(f"RSI={rsi:.0f} ({tag})")
        if ctx.get("volume_ratio") is not None:
            parts.append(f"volume {ctx['volume_ratio']:.1f}x average")
        return "; ".join(parts)

    # ── LLM call ─────────────────────────────────────────────────────────────
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not settings.ANTHROPIC_API_KEY:
            raise DataError(
                "ANTHROPIC_API_KEY not set — cannot run LLM ROI analysis. "
                "Add it to .env to enable Claude assessment.",
                severity="MEDIUM",
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise DataError(
                "anthropic package not installed (pip install anthropic)"
            ) from exc
        self._client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        return self._client

    def _build_user_prompt(
        self, roi: PutROIResult, ticker: str, ctx: dict
    ) -> str:
        def fmt(v, suffix: str = "", dash: str = "n/a") -> str:
            return f"{v}{suffix}" if v is not None else dash

        return f"""TICKER: {ticker}

OPTIONS DATA:
Strike: {roi.strike}
Expiry: {roi.expiry} ({roi.days_to_expiry} days)
Premium/contract: ${roi.premium_per_contract:.2f}
Margin/contract: ${roi.margin_per_contract:.2f} ({roi.margin_basis})
Static monthly ROI: {roi.monthly_roi_pct:.1f}%
Breakeven: ${roi.breakeven:.2f}

STOCK CONTEXT:
Current price: ${ctx.get('current_price')}
52w High: {fmt(ctx.get('high_52w'), '', '?')}  52w Low: {fmt(ctx.get('low_52w'), '', '?')}
% from 52w High: {fmt(ctx.get('pct_from_high'), '%')}
% from 52w Low: {fmt(ctx.get('pct_from_low'), '%')}
RSI(14): {fmt(ctx.get('rsi'))}
IV: {fmt(ctx.get('iv'), '%')}   HV(30d): {fmt(ctx.get('hv_30'), '%')}   IV/HV: {fmt(ctx.get('iv_vs_hv'), 'x')}
Volume vs avg: {fmt(ctx.get('volume_ratio'), 'x')}
News sentiment: {fmt(ctx.get('sentiment_score'))} (-1 to +1)
Short interest: {fmt(ctx.get('short_pct_float'), '%')} of float, {fmt(ctx.get('short_ratio_days'), ' days to cover')}
Earnings within 30 days: {fmt(ctx.get('has_earnings_within_30d'))} (next: {fmt(ctx.get('next_earnings_date'))})
Institutional accumulation: {fmt(ctx.get('institutional_accumulation'))}

Recent price action: {ctx.get('price_narrative') or 'n/a'}
"""

    def _call_llm(
        self, roi: PutROIResult, ticker: str, ctx: dict
    ) -> LLMAssessment:
        client = self._ensure_client()
        user_prompt = self._build_user_prompt(roi, ticker, ctx)
        logger.debug("LLM ROI prompt for %s:\n%s", ticker, user_prompt)

        last_exc: Exception | None = None
        for attempt in range(1, settings.API_MAX_RETRIES + 1):
            try:
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
                return self._parse_assessment(text)
            except Exception as exc:  # noqa: BLE001 — retry transient API errors
                last_exc = exc
                logger.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt, settings.API_MAX_RETRIES, exc,
                )
        raise DataError(
            f"LLM ROI analysis failed after {settings.API_MAX_RETRIES} attempts: {last_exc}",
            ticker=ticker,
        )

    @staticmethod
    def _parse_assessment(text: str) -> LLMAssessment:
        """Extract the JSON object from the model response and validate it."""
        raw = text.strip()
        # Strip markdown code fences if present.
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
        # Isolate the first {...} block.
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise DataError("LLM response contained no JSON object", response=text[:200])
        payload = raw[start : end + 1]
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DataError(
                f"Could not parse LLM JSON: {exc}", response=payload[:200]
            ) from exc
        return LLMAssessment(**data)


__all__ = [
    "PutROIResult",
    "LLMAssessment",
    "LLMROIAnalysis",
    "LLMROIAnalyzer",
    "Recommendation",
]
