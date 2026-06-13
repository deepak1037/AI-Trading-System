"""Earnings universe scanner (Phase 3, Step 5).

Full pipeline: pull upcoming earnings (Moomoo → CSV → FMP → yfinance), analyze
each ticker's IV pattern, pick a strategy (IV crush / spike / skip), build the
trade with real Schwab margin when a broker is present, get an LLM (or
rule-based) assessment, then score and rank. Returns opportunities sorted by
``overall_score`` descending.

All collaborators are injectable so the pipeline is unit-testable without any
network: ``EarningsScanner(provider=..., analyzer=..., builder=..., assessor=...)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from broker_client.earnings.iv_analyzer import IVHistoryAnalyzer
from broker_client.earnings.llm_assessor import LLMEarningsAssessor
from broker_client.earnings.models import (
    SKIP,
    EarningsEvent,
    EarningsOpportunity,
    IVAnalysis,
)
from broker_client.earnings.moomoo_earnings import EarningsDataProvider
from broker_client.earnings.strategy_builder import EarningsStrategyBuilder
from config.settings import settings
from core.logger import get_logger

if TYPE_CHECKING:
    from broker_core.base_broker import BaseBroker

logger = get_logger(__name__)


class EarningsScanner:
    """Scans upcoming earnings and ranks tradeable IV opportunities."""

    def __init__(
        self,
        broker: BaseBroker | None = None,
        provider: EarningsDataProvider | None = None,
        analyzer: IVHistoryAnalyzer | None = None,
        builder: EarningsStrategyBuilder | None = None,
        assessor: LLMEarningsAssessor | None = None,
    ) -> None:
        self.provider = provider or EarningsDataProvider()
        self.analyzer = analyzer or IVHistoryAnalyzer()
        self.builder = builder or EarningsStrategyBuilder(broker=broker)
        self.assessor = assessor or LLMEarningsAssessor()

    # ── public API ─────────────────────────────────────────────────────────────
    def scan(self, days_ahead: int | None = None) -> list[EarningsOpportunity]:
        days = days_ahead if days_ahead is not None else settings.EARNINGS_DAYS_AHEAD
        events = self.provider.get_upcoming_earnings(days)
        logger.info("Earnings scan: %d upcoming events in %d days", len(events), days)

        opportunities: list[EarningsOpportunity] = []
        for event in events:
            if not self._passes_minimum(event):
                continue
            opp = self._evaluate(event)
            if opp is not None:
                opportunities.append(opp)

        opportunities.sort(key=lambda o: o.overall_score, reverse=True)
        logger.info(
            "Earnings scan: %d opportunities (%d actionable)",
            len(opportunities),
            len([o for o in opportunities if o.strategy != SKIP]),
        )
        return opportunities

    def scan_ticker(self, ticker: str, days_ahead: int | None = None) -> EarningsOpportunity | None:
        """Evaluate a single named ticker if it has earnings in the window."""
        days = days_ahead if days_ahead is not None else settings.EARNINGS_DAYS_AHEAD
        for event in self.provider.get_upcoming_earnings(days):
            if event.ticker.upper() == ticker.upper():
                return self._evaluate(event)
        logger.info("No upcoming earnings found for %s in %d days", ticker, days)
        return None

    # ── per-event evaluation ────────────────────────────────────────────────────
    def _evaluate(self, event: EarningsEvent) -> EarningsOpportunity | None:
        analysis = self._analyze(event)
        trade = self.builder.build(event, analysis)
        assessment = self.assessor.assess(event.ticker, analysis, trade, event=event)
        score = self._score(analysis, trade, assessment)
        opp = EarningsOpportunity(
            ticker=event.ticker,
            earnings_date=event.earnings_date,
            earnings_time=event.earnings_time,
            strategy=analysis.strategy,
            iv_analysis=analysis,
            trade=trade,
            llm_assessment=assessment,
            overall_score=score,
            priority=self._priority(score, analysis.strategy),
        )
        return opp

    def _analyze(self, event: EarningsEvent) -> IVAnalysis:
        """Use full per-quarter history when available; else the summary path."""
        history = self.provider.get_iv_history(event.ticker)
        if history.count >= 1:
            return self.analyzer.analyze(
                event.ticker, history,
                iv_rank=event.iv_rank,
                iv_percentile=event.iv_percentile,
                expected_move_current=event.expected_move,
            )
        return self.analyzer.analyze_from_event(event)

    # ── filtering / scoring ──────────────────────────────────────────────────────
    @staticmethod
    def _passes_minimum(event: EarningsEvent) -> bool:
        """Cheap pre-filters (penny stocks, illiquid options)."""
        if event.stock_price is not None and event.stock_price < settings.EARNINGS_MIN_STOCK_PRICE:
            return False
        if (
            event.options_volume is not None
            and event.options_volume < settings.EARNINGS_MIN_OPTIONS_VOLUME
        ):
            return False
        if (
            event.open_interest is not None
            and event.open_interest < settings.EARNINGS_MIN_OPEN_INTEREST
        ):
            return False
        return True

    @staticmethod
    def _score(analysis: IVAnalysis, trade, assessment) -> int:
        """Blend IV-analysis confidence, LLM confidence, and the recommendation."""
        if analysis.strategy == SKIP or trade is None:
            return 0
        base = analysis.confidence
        if assessment is not None:
            blended = (base + assessment.confidence) / 2.0
            mult = {
                "EXECUTE": 1.0, "REDUCE_SIZE": 0.7, "SKIP": 0.2,
            }.get(assessment.recommendation, 0.5)
            base = blended * mult
        return int(round(max(0.0, min(base, 100.0))))

    @staticmethod
    def _priority(score: int, strategy: str) -> str:
        if strategy == SKIP or score <= 0:
            return "SKIP"
        if score >= 75:
            return "HIGH"
        if score >= 55:
            return "MEDIUM"
        if score >= 40:
            return "LOW"
        return "SKIP"

    # ── persistence (best-effort) ────────────────────────────────────────────────
    def save(self, opportunities: list[EarningsOpportunity], db_path: str | None = None) -> int:
        """Persist opportunities to ``earnings_opportunities``. Returns rows saved."""
        try:
            from data.db import get_connection

            saved = 0
            with get_connection(db_path) as conn:
                for o in opportunities:
                    conn.execute(
                        """INSERT INTO earnings_opportunities
                           (ticker, earnings_date, earnings_time, strategy, iv_rank,
                            expected_move, avg_iv_crush, breach_rate, overall_score,
                            priority, trade_json, llm_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            o.ticker, o.earnings_date.isoformat(), o.earnings_time,
                            o.strategy, o.iv_analysis.iv_rank_current,
                            o.iv_analysis.expected_move_current, o.iv_analysis.avg_iv_crush,
                            o.iv_analysis.breach_rate, o.overall_score, o.priority,
                            o.trade.model_dump_json() if o.trade else None,
                            o.llm_assessment.model_dump_json() if o.llm_assessment else None,
                        ),
                    )
                    saved += 1
            return saved
        except Exception as exc:  # noqa: BLE001 — persistence is non-critical
            logger.warning("Failed to save earnings opportunities: %s", exc)
            return 0


__all__ = ["EarningsScanner"]
