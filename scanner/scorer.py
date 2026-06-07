"""Scanner composite scorer (Days 13-18).

Scoring weights (CLAUDE.md Section 14):
  fundamental: 30%
  institutional: 25%
  technical: 20%
  squeeze (short interest setup): 15%
  timeframe alignment: 10%

Minimum score to enter watchlist: SCANNER_WATCHLIST_THRESHOLD (default 65)
"""

from __future__ import annotations

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class CompositeScorer:
    """Compute 0-100 composite score from individual signal components."""

    WEIGHTS = {
        "fundamental": 30,
        "institutional": 25,
        "technical": 20,
        "squeeze": 15,
        "tf_alignment": 10,
    }

    def score(
        self,
        fundamental_score: float = 0.0,
        institutional_score: float = 0.0,
        technical_score: float = 0.0,
        squeeze_score: float = 0.0,
        tf_alignment_score: float = 0.0,
    ) -> int:
        """Compute weighted composite score 0-100.

        Each input component is expected to be 0-100.
        """
        raw = (
            fundamental_score * self.WEIGHTS["fundamental"] / 100
            + institutional_score * self.WEIGHTS["institutional"] / 100
            + technical_score * self.WEIGHTS["technical"] / 100
            + squeeze_score * self.WEIGHTS["squeeze"] / 100
            + tf_alignment_score * self.WEIGHTS["tf_alignment"] / 100
        )
        return int(max(0, min(100, round(raw))))

    def fundamental_score_from_flags(
        self,
        eps_accelerating: bool,
        rev_reaccelerating: bool,
        est_revisions_up: bool,
    ) -> float:
        """Convert fundamental pass/fail flags to 0-100 score."""
        score = 0.0
        if eps_accelerating:
            score += 50.0
        if rev_reaccelerating:
            score += 30.0
        if est_revisions_up:
            score += 20.0
        return score

    def institutional_score_from_data(
        self,
        holder_count: int,
        pct_held: float,
        form4_buys: int,
    ) -> float:
        """Convert institutional accumulation data to 0-100 score."""
        score = 0.0
        # Holder count (more = better, cap at 100)
        score += min(50.0, holder_count * 1.0)
        # % held by institutions
        score += min(30.0, pct_held * 100)
        # Insider buying
        score += min(20.0, form4_buys * 5.0)
        return min(100.0, score)

    def technical_score_from_flags(
        self,
        is_stage2: bool,
        in_base: bool,
        rs_rank: float,
    ) -> float:
        """Convert technical analysis flags to 0-100 score."""
        score = 0.0
        if is_stage2:
            score += 40.0
        if in_base:
            score += 20.0
        # RS rank contribution (0-40 based on percentile)
        score += min(40.0, rs_rank * 0.40)
        return min(100.0, score)

    def squeeze_score_from_short(self, short_ratio: float, short_interest_pct: float) -> float:
        """Compute short squeeze setup score (high short = high squeeze potential)."""
        # Short ratio > 5 days to cover = interesting squeeze candidate
        if short_ratio <= 0:
            return 0.0
        ratio_score = min(60.0, short_ratio * 6.0)
        pct_score = min(40.0, short_interest_pct * 2.0)
        return min(100.0, ratio_score + pct_score)

    def passes_watchlist_threshold(self, composite: int) -> bool:
        return composite >= settings.SCANNER_WATCHLIST_THRESHOLD


__all__ = ["CompositeScorer"]
