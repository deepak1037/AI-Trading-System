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
    ROI percentages use *net* premium (gross − commission) over the capital
    requirement, scaled to the stated horizon. Two capital bases are reported:
    the *primary* margin (Schwab's real buying-power reduction when available,
    else a Reg-T estimate) and the *cash-secured* basis (strike × multiplier).
    """

    ticker: str
    strike: float
    expiry: str                       # YYYY-MM-DD
    days_to_expiry: int
    underlying_price: float

    bid: float
    ask: float
    mid: float                        # per share
    delta: Optional[float] = None     # contract delta (≈ assignment prob), guarded
    iv: Optional[float] = None        # contract implied vol, %, guarded

    gross_premium_per_contract: float     # mid * multiplier
    commission_per_contract: float        # per-contract commission/fees
    premium_per_contract: float           # net = gross − commission

    margin_per_contract: float            # primary capital tied up per contract
    margin_basis: str                     # schwab_preview | reg_t | cash_secured

    breakeven: float                  # strike − net premium/share
    otm_pct: float                    # % the strike sits below spot (negative = ITM)

    static_roi_pct: float             # net premium / margin over the holding period
    monthly_roi_pct: float            # static scaled to 30 days
    annualized_roi_pct: float         # static scaled to 365 days

    # Cash-secured companion (capital = strike × multiplier).
    cash_secured_margin_per_contract: float
    cash_secured_roi_pct: float
    cash_secured_monthly_roi_pct: float
    cash_secured_annualized_roi_pct: float


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
        """End-to-end: build the ROI result then run the LLM assessment.

        Uses the broker's real buying-power reduction for margin (via the
        preview API) when a broker is available.
        """
        roi = self.build_put_roi(ticker, strike, expiry, use_broker_margin=True)
        return self.analyze_put_opportunity(roi, ticker)

    def list_expiries(self, ticker: str, limit: int = 12) -> list[str]:
        """Return available put expiries (YYYY-MM-DD), soonest first.

        Prefers the broker's chain (one authenticated call, no rate limit);
        falls back to yfinance only when no broker is configured or the broker
        call fails. yfinance's ``.options`` endpoint rate-limits aggressively,
        so the broker path is strongly preferred when available.
        """
        ticker = ticker.upper().strip()
        if self._broker is not None:
            try:
                chain = self._broker.get_options_chain(ticker)
                exps = sorted({p.expiry for p in chain.puts if p.expiry})
                if exps:
                    return exps[:limit]
            except Exception as exc:  # noqa: BLE001 — fall back to yfinance
                logger.debug("Broker expiry list failed for %s: %s", ticker, exc)
        try:
            import yfinance as yf

            return list(yf.Ticker(ticker).options or [])[:limit]
        except Exception as exc:  # noqa: BLE001
            logger.debug("yfinance expiry list failed for %s: %s", ticker, exc)
            return []

    def build_roi_table(
        self,
        ticker: str,
        strike: float,
        limit: int = 12,
        underlying_price: Optional[float] = None,
        use_broker_margin: bool = False,
    ) -> list[PutROIResult]:
        """ROI for a strike across all expiries, from a SINGLE chain fetch.

        Fetches the broker's full chain once and computes per-expiry ROI from
        the in-memory contracts — fast enough to render a table without hitting
        rate limits. Picks the exact strike at each expiry, or the closest
        listed strike. Returns [] when no broker is configured (caller should
        fall back to a per-expiry path).

        Args:
            use_broker_margin: When True, each row's margin is Schwab's real
                buying-power reduction (one preview call per expiry — slower but
                accurate). Account buying power is fetched once and reused across
                rows. When False (default), each row uses the fast Reg-T formula.
        """
        ticker = ticker.upper().strip()
        if self._broker is None:
            return []
        try:
            chain = self._broker.get_options_chain(ticker)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Broker chain fetch failed for %s: %s", ticker, exc)
            return []
        if underlying_price is None:
            underlying_price = self._current_price(ticker)

        # Fetch account available funds once so per-row previews don't re-fetch.
        current_af: Optional[float] = None
        if use_broker_margin:
            try:
                current_af = float(self._broker.get_account().available_funds or 0.0)
            except Exception as exc:  # noqa: BLE001 — rows fall back to formula
                logger.debug("Account fetch failed for %s: %s", ticker, exc)

        by_expiry: dict[str, list[OptionsContract]] = {}
        for put in chain.puts:
            if put.expiry:
                by_expiry.setdefault(put.expiry, []).append(put)

        results: list[PutROIResult] = []
        for expiry in sorted(by_expiry)[:limit]:
            puts = [p for p in by_expiry[expiry] if (p.bid or p.ask or p.last)]
            if not puts:
                continue
            exact = [p for p in puts if abs(p.strike - strike) < 1e-6]
            contract = exact[0] if exact else min(
                puts, key=lambda p: abs(p.strike - strike)
            )
            try:
                results.append(
                    self.build_put_roi(
                        ticker,
                        contract.strike,
                        expiry,
                        contract=contract,
                        underlying_price=underlying_price,
                        use_broker_margin=use_broker_margin,
                        current_available_funds=current_af,
                    )
                )
            except DataError:
                continue
        return results

    def build_put_roi(
        self,
        ticker: str,
        strike: float,
        expiry: str,
        contract: Optional[OptionsContract] = None,
        underlying_price: Optional[float] = None,
        use_broker_margin: bool = False,
        current_available_funds: Optional[float] = None,
    ) -> PutROIResult:
        """Locate the put contract and compute its quantitative ROI.

        Args:
            use_broker_margin: When True and a broker is configured, query the
                broker's preview API for the *real* available-funds reduction
                and use it as the primary margin. Falls back to the Reg-T /
                cash-secured formula when unavailable (paper/dev, no broker, or
                an invalid preview). Off by default because it adds an API
                round-trip per contract — callers scanning many expiries should
                leave it off; the single deep analysis turns it on.
            current_available_funds: Optional pre-fetched account available
                funds, so a caller computing many contracts (build_roi_table)
                avoids one get_account() call per contract. Ignored unless
                use_broker_margin is True.
        """
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

        mult = self._multiplier
        gross_premium = mid * mult
        commission = settings.PAPER_OPTIONS_COMMISSION  # per contract
        net_premium = gross_premium - commission
        net_premium_per_share = net_premium / mult

        # ── Primary margin: real (broker preview) with formula fallback ───────
        margin_basis = settings.OPTIONS_MARGIN_BASIS
        margin = None
        if use_broker_margin:
            margin = self._broker_margin_per_contract(
                ticker, contract, mid,
                current_available_funds=current_available_funds,
            )
            if margin is not None:
                margin_basis = "schwab_preview"
        if margin is None:
            margin = self._margin_per_contract(strike, underlying_price, mid)

        days = self._days_to_expiry(expiry)
        scale = max(days, 1)

        def _roi(capital: float) -> tuple[float, float, float]:
            static = (net_premium / capital * 100.0) if capital > 0 else 0.0
            return static, static * (30.0 / scale), static * (365.0 / scale)

        static_roi, monthly_roi, annual_roi = _roi(margin)

        # ── Cash-secured companion (capital = strike × multiplier) ────────────
        cs_margin = strike * mult
        cs_static, cs_monthly, cs_annual = _roi(cs_margin)

        otm_pct = (
            (underlying_price - strike) / underlying_price * 100.0
            if underlying_price > 0
            else 0.0
        )
        delta = self._clean_greek(contract.delta)
        iv = self._normalize_iv(contract.implied_vol)

        result = PutROIResult(
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            days_to_expiry=days,
            underlying_price=round(underlying_price, 2),
            bid=round(contract.bid, 2),
            ask=round(contract.ask, 2),
            mid=round(mid, 2),
            delta=round(delta, 4) if delta is not None else None,
            iv=iv,
            gross_premium_per_contract=round(gross_premium, 2),
            commission_per_contract=round(commission, 2),
            premium_per_contract=round(net_premium, 2),
            margin_per_contract=round(margin, 2),
            margin_basis=margin_basis,
            breakeven=round(strike - net_premium_per_share, 2),
            otm_pct=round(otm_pct, 2),
            static_roi_pct=round(static_roi, 2),
            monthly_roi_pct=round(monthly_roi, 2),
            annualized_roi_pct=round(annual_roi, 2),
            cash_secured_margin_per_contract=round(cs_margin, 2),
            cash_secured_roi_pct=round(cs_static, 2),
            cash_secured_monthly_roi_pct=round(cs_monthly, 2),
            cash_secured_annualized_roi_pct=round(cs_annual, 2),
        )
        logger.info(
            "PutROI %s $%s %s: net_premium=$%.0f margin=$%.0f (%s) "
            "monthly=%.1f%% | cash-secured monthly=%.1f%%",
            ticker, strike, expiry, net_premium, margin, margin_basis,
            monthly_roi, cs_monthly,
        )
        return result

    def _broker_margin_per_contract(
        self,
        ticker: str,
        contract: OptionsContract,
        mid: float,
        current_available_funds: Optional[float] = None,
    ) -> Optional[float]:
        """Real per-contract margin via the broker preview API.

        The true capital tied up is the drop in **available funds** Schwab
        projects for the order: ``availableFunds − projectedAvailableFund``.
        This is the reference tool's approach. We deliberately use available
        funds, NOT buying power: buying power is leveraged (~4× funds on a
        margin account), so its delta overstates the real margin several-fold
        (e.g. HIMS $23 put: $336 funds-delta vs $1,345 buying-power-delta).

        Returns None on any failure (no broker, missing funds data, non-positive
        reduction) so the caller falls back to the formula. ``current_available_funds``
        may be passed pre-fetched to avoid one get_account() call per contract.
        """
        if self._broker is None:
            return None
        try:
            from broker_core.base_broker import OptionsOrder

            if current_available_funds is None:
                account = self._broker.get_account()
                current_available_funds = float(account.available_funds or 0.0)
            if current_available_funds <= 0:
                return None
            order = OptionsOrder(
                ticker=ticker,
                action="SELL_TO_OPEN",
                contract=contract.symbol,
                qty=1,
                order_type="limit",
                limit_price=round(mid, 2),
                strategy_name="roi_preview",
            )
            preview = self._broker.preview_options_order(order)
            # NOTE: do NOT gate on preview.is_valid. Schwab attaches benign
            # informational *alerts* (e.g. the naked-put acknowledgment) to put
            # previews, which flip is_valid to False even though the projected
            # balance is perfectly usable. The balance projection is what we
            # need; a real reject simply yields a non-positive/zero projection.
            if preview.rejection_reason:
                logger.debug(
                    "Preview note for %s margin: %s",
                    contract.symbol, preview.rejection_reason,
                )
            projected_af = float(preview.projected_available_fund or 0.0)
            if projected_af <= 0:
                return None
            reduction = current_available_funds - projected_af
            # Sanity: a single contract can't tie up more than all available funds.
            if 0 < reduction < current_available_funds:
                return reduction
            logger.debug(
                "Implausible funds reduction (%.2f) for %s — using formula",
                reduction, contract.symbol,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — degrade to formula
            logger.debug("Broker margin preview failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _clean_greek(value: Optional[float]) -> Optional[float]:
        """Reject Schwab's ``±999`` missing-data sentinels for Greeks/IV."""
        if value is None:
            return None
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None
        if abs(fv) >= 900:
            return None
        return fv

    @staticmethod
    def _normalize_iv(raw: Optional[float]) -> Optional[float]:
        """Return implied vol as a percent, normalizing across data sources.

        Schwab's ``volatility`` field is already a percent (70.82 = 70.82%);
        yfinance's ``impliedVolatility`` is a fraction (0.62 = 62%). Values < 3
        are treated as fractions and scaled up; ``±999`` sentinels are dropped.
        """
        iv = LLMROIAnalyzer._clean_greek(raw)
        if iv is None or iv <= 0:
            return None
        return round(iv, 1) if iv >= 3 else round(iv * 100.0, 1)

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
            "delta": roi.delta,   # contract delta from the chain (≈ assignment prob)
            "iv": roi.iv,         # contract IV from the chain (%), if present
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
        # If the chain already supplied IV, just compute IV/HV and skip the
        # extra yfinance round-trip.
        if ctx.get("iv") is not None:
            hv = ctx.get("hv_30")
            if hv:
                ctx["iv_vs_hv"] = round(ctx["iv"] / hv, 2)
            return
        try:
            import yfinance as yf

            opt = yf.Ticker(ticker)
            chain = opt.option_chain(roi.expiry) if roi.expiry else None
            iv = None
            if chain is not None:
                puts = chain.puts
                row = puts[abs(puts["strike"] - roi.strike) < 1e-6]
                if not row.empty and "impliedVolatility" in row:
                    iv = LLMROIAnalyzer._normalize_iv(float(row["impliedVolatility"].iloc[0]))
            if iv is not None:
                ctx["iv"] = iv
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
Strike: {roi.strike}  ({roi.otm_pct:.1f}% OTM)
Expiry: {roi.expiry} ({roi.days_to_expiry} days)
Bid/Ask/Mid: ${roi.bid:.2f} / ${roi.ask:.2f} / ${roi.mid:.2f}
Contract delta: {fmt(roi.delta)}  (≈ assignment probability)
Gross premium/contract: ${roi.gross_premium_per_contract:.2f}
Commission/contract: ${roi.commission_per_contract:.2f}
Net premium/contract: ${roi.premium_per_contract:.2f}
Margin/contract: ${roi.margin_per_contract:.2f} ({roi.margin_basis})
Static monthly ROI (margin): {roi.monthly_roi_pct:.1f}%  (annualized {roi.annualized_roi_pct:.1f}%)
Cash-secured monthly ROI: {roi.cash_secured_monthly_roi_pct:.1f}% (capital ${roi.cash_secured_margin_per_contract:.0f})
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
        import anthropic

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
            except anthropic.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                # Retry only transient server-side conditions (429 / 5xx).
                if status == 429 or (status is not None and status >= 500):
                    last_exc = exc
                    logger.warning(
                        "LLM transient error %s (attempt %d/%d)",
                        status, attempt, settings.API_MAX_RETRIES,
                    )
                    continue
                # Non-transient (4xx) — surface immediately with a clear message.
                msg = str(getattr(exc, "message", "") or exc)
                if "credit balance" in msg.lower():
                    raise DataError(
                        "Anthropic API credit balance is too low — add credits at "
                        "console.anthropic.com (Plans & Billing) to enable the Claude "
                        "assessment. The ROI table above does not need it.",
                        ticker=ticker,
                    ) from exc
                raise DataError(
                    f"Anthropic API rejected the request ({status}): {msg}",
                    ticker=ticker,
                ) from exc
            except Exception as exc:  # noqa: BLE001 — retry transient (conn/timeout/parse)
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
