"""IV history analyzer (Phase 3, Step 2).

Turns a ticker's per-quarter earnings history (or, when only summary data is
available, a single ``EarningsEvent``) into an ``IVAnalysis``: the average IV
crush, how consistently it crushes, how often the stock breaches the expected
move, the historically "safe" put-strike distance, and a strategy verdict
(``IV_CRUSH`` / ``IV_SPIKE`` / ``SKIP``).

All thresholds come from ``settings`` (Phase 3 block) — nothing hardcoded.

Two entry points:
  * ``analyze(ticker, history)`` — full per-quarter analysis (richest).
  * ``analyze_from_event(event)`` — degraded path from Moomoo summary columns
    when no per-quarter history is available (CSV export, FMP, etc).
"""

from __future__ import annotations

import numpy as np

from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsEvent,
    EarningsIVHistory,
    IVAnalysis,
    QuarterlyData,
)
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class IVHistoryAnalyzer:
    """Analyses earnings IV-crush and breach-rate patterns."""

    # ── public API ─────────────────────────────────────────────────────────────
    def analyze(
        self,
        ticker: str,
        history: EarningsIVHistory,
        iv_rank: int = 0,
        iv_percentile: int = 0,
        expected_move_current: float = 0.0,
    ) -> IVAnalysis:
        """Full per-quarter analysis. Falls back to defaults on empty history."""
        quarters = history.quarters
        crush = self._analyze_iv_crush(quarters)
        breach = self._analyze_breach_rate(quarters)

        analysis = IVAnalysis(
            ticker=ticker,
            avg_iv_crush=round(crush["avg_iv_crush"], 2),
            last_iv_crush=round(crush["last_iv_crush"], 2),
            iv_crush_consistency=round(crush["crush_consistency"], 3),
            avg_expected_move=round(breach["avg_expected_move"], 2),
            avg_actual_move=round(breach["avg_actual_move"], 2),
            breach_rate=round(breach["breach_rate"], 3),
            breach_rate_upper=round(breach["breach_rate_upper"], 3),
            breach_rate_lower=round(breach["breach_rate_lower"], 3),
            safe_move_level=round(breach["safe_move_level"], 2),
            iv_rank_current=iv_rank,
            iv_percentile_current=iv_percentile,
            expected_move_current=round(expected_move_current, 2),
            quarters_analyzed=len([q for q in quarters if q.iv_crush is not None]),
        )
        self._decide_strategy(analysis)
        return analysis

    def analyze_from_event(self, event: EarningsEvent) -> IVAnalysis:
        """Degraded analysis from Moomoo summary columns (no per-quarter data).

        Builds a single-sample breach estimate from the last actual move vs the
        expected move, and proxies crush consistency from whether the historical
        crush clears the "meaningful" threshold. Marked with quarters_analyzed=0
        so callers can treat it as lower-confidence.
        """
        last_move = abs(event.last_earnings_move)
        expected = event.expected_move
        breached_last = expected > 0 and last_move > expected
        consistency = (
            0.7 if event.hist_iv_crush >= settings.EARNINGS_IV_CRUSH_MEANINGFUL_PCT
            else 0.3
        )
        analysis = IVAnalysis(
            ticker=event.ticker,
            avg_iv_crush=round(event.hist_iv_crush, 2),
            last_iv_crush=round(event.last_iv_crush, 2),
            iv_crush_consistency=consistency,
            avg_expected_move=round(expected, 2),
            avg_actual_move=round(last_move, 2),
            breach_rate=1.0 if breached_last else 0.0,
            breach_rate_upper=1.0 if (breached_last and event.last_earnings_move > 0) else 0.0,
            breach_rate_lower=1.0 if (breached_last and event.last_earnings_move < 0) else 0.0,
            safe_move_level=round(max(last_move, expected), 2),
            iv_rank_current=event.iv_rank,
            iv_percentile_current=event.iv_percentile,
            expected_move_current=round(expected, 2),
            quarters_analyzed=0,
        )
        self._decide_strategy(analysis)
        return analysis

    # ── IV crush analysis ──────────────────────────────────────────────────────
    def _analyze_iv_crush(self, history: list[QuarterlyData]) -> dict:
        """Average IV crush + how consistently it crushes meaningfully."""
        crushes = [q.iv_crush for q in history if q.iv_crush is not None]
        if not crushes:
            return {"avg_iv_crush": 0.0, "last_iv_crush": 0.0, "crush_consistency": 0.0}

        avg_crush = float(np.mean(crushes))
        meaningful = settings.EARNINGS_IV_CRUSH_MEANINGFUL_PCT
        consistency = len([c for c in crushes if c > meaningful]) / len(crushes)
        # First quarter in the list is the most recent (Moomoo orders newest-first).
        return {
            "avg_iv_crush": avg_crush,
            "last_iv_crush": float(crushes[0]),
            "crush_consistency": consistency,
        }

    # ── breach-rate analysis ───────────────────────────────────────────────────
    def _analyze_breach_rate(self, history: list[QuarterlyData]) -> dict:
        """What fraction of quarters moved MORE than the expected move?

        Uses ``actual_move_close`` (signed) as the reference, matching Moomoo's
        "Actual Move from Close" column.
        """
        # Concrete (signed actual move, expected move) pairs — no Optionals,
        # so downstream arithmetic is unambiguous.
        pairs: list[tuple[float, float]] = [
            (q.actual_move_close, q.expected_move)
            for q in history
            if q.actual_move_close is not None and q.expected_move is not None
        ]
        if not pairs:
            return {
                "avg_expected_move": 0.0, "avg_actual_move": 0.0, "breach_rate": 0.0,
                "breach_rate_upper": 0.0, "breach_rate_lower": 0.0, "safe_move_level": 0.0,
            }

        n = len(pairs)
        breaches = [(actual, exp) for actual, exp in pairs if abs(actual) > exp]
        upper = [a for a, _ in breaches if a > 0]
        lower = [a for a, _ in breaches if a < 0]
        actual_moves = [abs(a) for a, _ in pairs]

        return {
            "avg_expected_move": float(np.mean([exp for _, exp in pairs])),
            "avg_actual_move": float(np.mean(actual_moves)),
            "breach_rate": len(breaches) / n,
            "breach_rate_upper": len(upper) / n,
            "breach_rate_lower": len(lower) / n,
            "safe_move_level": self._safe_move_from(actual_moves),
        }

    @classmethod
    def _calculate_safe_put_strike(
        cls, quarters: list[QuarterlyData], safety_pct: float | None = None
    ) -> float:
        """The move magnitude (%) that contained ``safety_pct`` of actual moves.

        Example: moves of 8/5/12/3/7/15/4% → 85th percentile ≈ 13.2%, so a put
        placed ~13% OTM would have survived 85% of these quarters.
        """
        moves = [abs(q.actual_move_close) for q in quarters if q.actual_move_close is not None]
        return cls._safe_move_from(moves, safety_pct)

    @staticmethod
    def _safe_move_from(moves: list[float], safety_pct: float | None = None) -> float:
        pct = safety_pct if safety_pct is not None else settings.EARNINGS_SAFE_PERCENTILE
        if not moves:
            return 0.0
        return float(np.percentile(moves, pct * 100))

    # ── strategy decision ──────────────────────────────────────────────────────
    def _decide_strategy(self, a: IVAnalysis) -> None:
        """Set ``a.strategy`` / ``confidence`` / ``reasoning`` in place.

        When per-quarter IV-crush history is unavailable (Moomoo serves current IV
        rank but no crush table), decide on IV rank alone via the "lite" path — an
        elevated IV rank IS the crush setup; a low one IS the spike setup — at a
        capped, clearly-flagged confidence.
        """
        if not self._has_crush_history(a) and a.iv_rank_current > 0:
            self._decide_lite(a)
            return
        if self._should_sell_iv_crush(a):
            a.strategy = IV_CRUSH
            a.confidence = self._crush_confidence(a)
            a.reasoning = (
                f"IV crush: avg {a.avg_iv_crush:.0f}% over "
                f"{a.iv_crush_consistency:.0%} of quarters, downside breach "
                f"{a.breach_rate_lower:.0%} ≤ {settings.EARNINGS_MAX_BREACH_RATE:.0%}, "
                f"IV rank {a.iv_rank_current} elevated — sell premium"
                + (" (size down for breach)" if a.breach_rate_lower >= 0.20 else "")
                + "."
            )
        elif self._should_buy_iv_spike(a):
            a.strategy = IV_SPIKE
            a.confidence = self._spike_confidence(a)
            a.reasoning = (
                f"IV low (rank {a.iv_rank_current}) but stock moves big "
                f"(avg {a.avg_actual_move:.0f}%, breach {a.breach_rate:.0%}) — "
                "buy premium for IV expansion, exit before earnings."
            )
        else:
            a.strategy = SKIP
            a.confidence = 0
            a.reasoning = self._skip_reason(a)

    @staticmethod
    def _has_crush_history(a: IVAnalysis) -> bool:
        """True when we have real per-quarter crush data to reason from."""
        return a.quarters_analyzed > 0 or a.avg_iv_crush > 0

    def _decide_lite(self, a: IVAnalysis) -> None:
        """IV-rank-only decision when no crush history is available.

        Confidence is capped (max ~70) and the reasoning flags the missing crush
        history, so these never outrank a fully-backed analysis.
        """
        rank = a.iv_rank_current
        pct = a.iv_percentile_current
        if (
            rank >= settings.EARNINGS_IV_RANK_SELL_THRESHOLD
            and pct >= settings.EARNINGS_IV_PERCENTILE_SELL_THRESHOLD
        ):
            a.strategy = IV_CRUSH
            a.confidence = int(min(45 + (rank - 50) * 0.5, 70))
            a.reasoning = (
                f"IV rank {rank}/100 elevated (pct {pct}); no per-quarter crush "
                "history — sell premium into the expected post-earnings IV collapse. "
                "Lower confidence than a crush-history-backed setup."
            )
        elif rank <= settings.EARNINGS_IV_RANK_BUY_THRESHOLD:
            a.strategy = IV_SPIKE
            a.confidence = int(min(45 + (35 - rank) * 0.7, 70))
            a.reasoning = (
                f"IV rank {rank}/100 low; no crush history — buy premium for "
                "pre-earnings IV expansion and exit before the print. Lower "
                "confidence than a history-backed setup."
            )
        else:
            a.strategy = SKIP
            a.confidence = 0
            a.reasoning = (
                f"IV rank {rank}/100 in no-man's-land and no crush history to refine."
            )

    @staticmethod
    def _should_sell_iv_crush(a: IVAnalysis) -> bool:
        # The IV-crush trade sells an OTM PUT, so only a DOWNSIDE breach threatens
        # it — an upside rip leaves the put worthless (you keep the premium). Judge
        # on breach_rate_lower (the total breach_rate would unfairly penalize a
        # stock that only ever breaks to the upside).
        return all([
            a.iv_rank_current >= settings.EARNINGS_IV_RANK_SELL_THRESHOLD,
            a.iv_percentile_current >= settings.EARNINGS_IV_PERCENTILE_SELL_THRESHOLD,
            a.avg_iv_crush >= settings.EARNINGS_MIN_IV_CRUSH_PCT,
            a.iv_crush_consistency >= settings.EARNINGS_IV_CRUSH_CONSISTENCY,
            a.breach_rate_lower <= settings.EARNINGS_MAX_BREACH_RATE,
        ])

    @staticmethod
    def _should_buy_iv_spike(a: IVAnalysis) -> bool:
        return all([
            a.iv_rank_current <= settings.EARNINGS_IV_RANK_BUY_THRESHOLD,
            a.iv_percentile_current <= settings.EARNINGS_IV_PERCENTILE_BUY_THRESHOLD,
            a.avg_actual_move >= settings.EARNINGS_MIN_ACTUAL_MOVE_FOR_SPIKE,
            a.breach_rate >= settings.EARNINGS_MIN_BREACH_RATE,
        ])

    @staticmethod
    def _crush_confidence(a: IVAnalysis) -> int:
        """0-100 — stronger crush, higher consistency, lower DOWNSIDE breach → higher.

        Uses downside breach (what threatens a short put), scaled to ~15% so a
        clean crusher scores high and a borderline one (≈25-30%) lands mid-range
        rather than zero.
        """
        crush_score = min(a.avg_iv_crush / 20.0, 1.0) * 40      # up to 40 pts
        consistency_score = a.iv_crush_consistency * 30          # up to 30 pts
        breach_score = (1.0 - min(a.breach_rate_lower / 0.15, 1.0)) * 30  # up to 30 pts
        return int(round(crush_score + consistency_score + breach_score))

    @staticmethod
    def _spike_confidence(a: IVAnalysis) -> int:
        move_score = min(a.avg_actual_move / 15.0, 1.0) * 50     # up to 50 pts
        breach_score = min(a.breach_rate / 0.6, 1.0) * 30        # up to 30 pts
        low_iv_score = (1.0 - a.iv_rank_current / 35.0) * 20 if a.iv_rank_current <= 35 else 0
        return int(round(move_score + breach_score + max(low_iv_score, 0)))

    @staticmethod
    def _skip_reason(a: IVAnalysis) -> str:
        reasons: list[str] = []
        if a.iv_rank_current < settings.EARNINGS_IV_RANK_SELL_THRESHOLD and (
            a.iv_rank_current > settings.EARNINGS_IV_RANK_BUY_THRESHOLD
        ):
            reasons.append(f"IV rank {a.iv_rank_current} in no-man's-land")
        if a.avg_iv_crush < settings.EARNINGS_MIN_IV_CRUSH_PCT:
            reasons.append(f"weak avg crush {a.avg_iv_crush:.0f}%")
        if (
            settings.EARNINGS_MAX_BREACH_RATE < a.breach_rate
            < settings.EARNINGS_MIN_BREACH_RATE
        ):
            reasons.append(f"breach rate {a.breach_rate:.0%} neither safe nor explosive")
        return "Skip — " + ("; ".join(reasons) if reasons else "no edge in IV pattern")


__all__ = ["IVHistoryAnalyzer"]
