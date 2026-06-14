"""Tests for the Phase 3 IV history analyzer (crush + breach rate)."""

from __future__ import annotations

import numpy as np

from broker_client.earnings.iv_analyzer import IVHistoryAnalyzer
from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsEvent,
    EarningsIVHistory,
    QuarterlyData,
)


def _hist(ticker: str, rows: list[tuple[float, float, float]]) -> EarningsIVHistory:
    """rows = list of (iv_crush, expected_move, actual_move_close)."""
    quarters = [
        QuarterlyData(
            quarter=f"Q{i}", iv_crush=c, expected_move=em, actual_move_close=am
        )
        for i, (c, em, am) in enumerate(rows)
    ]
    return EarningsIVHistory(ticker=ticker, quarters=quarters)


def test_high_iv_crush_detected() -> None:
    # Consistent ~25% crush, moves well inside expected → reliable crusher.
    hist = _hist("NVDA", [(25, 8, 4), (28, 8, -3), (22, 8, 5), (26, 8, 2), (24, 8, -4), (27, 8, 3)])
    a = IVHistoryAnalyzer().analyze("NVDA", hist, iv_rank=70, iv_percentile=70)
    assert a.strategy == IV_CRUSH
    assert a.avg_iv_crush >= 15
    assert a.iv_crush_consistency == 1.0
    assert a.breach_rate == 0.0
    assert a.confidence > 50


def test_low_iv_spike_detected() -> None:
    # IV low now, but the stock historically moves big and breaches often.
    hist = _hist("TSLA", [(10, 6, 12), (8, 6, -11), (9, 6, 14), (7, 6, -9), (11, 6, 13), (6, 6, 10)])
    a = IVHistoryAnalyzer().analyze("TSLA", hist, iv_rank=20, iv_percentile=30)
    assert a.strategy == IV_SPIKE
    assert a.avg_actual_move >= 8
    assert a.breach_rate >= 0.40
    assert a.confidence > 0


def test_breach_rate_calculation() -> None:
    # 2 of 4 quarters exceed the expected move → breach_rate 0.5.
    hist = _hist("X", [(20, 5, 8), (20, 5, -3), (20, 5, 9), (20, 5, 2)])
    a = IVHistoryAnalyzer().analyze("X", hist, iv_rank=55, iv_percentile=65)
    assert a.breach_rate == 0.5
    assert a.breach_rate_upper == 0.5   # both breaches were upside (+8, +9)
    assert a.breach_rate_lower == 0.0


def test_safe_put_strike_selection() -> None:
    moves = [8, 5, 12, 3, 7, 15, 4]
    quarters = [QuarterlyData(expected_move=5, actual_move_close=-m) for m in moves]
    safe = IVHistoryAnalyzer._calculate_safe_put_strike(quarters, safety_pct=0.85)
    assert abs(safe - float(np.percentile(moves, 85))) < 1e-6
    assert 12 < safe < 15  # 85th pct sits between the 12% and 15% moves


def test_skip_when_criteria_not_met() -> None:
    # Middling IV rank, weak crush, mid breach → no edge.
    hist = _hist("MEH", [(8, 5, 6), (9, 5, 4), (7, 5, 7), (10, 5, 3)])
    a = IVHistoryAnalyzer().analyze("MEH", hist, iv_rank=42, iv_percentile=45)
    assert a.strategy == SKIP
    assert a.confidence == 0
    assert "Skip" in a.reasoning


def test_empty_history_is_skip() -> None:
    a = IVHistoryAnalyzer().analyze("NONE", EarningsIVHistory(ticker="NONE", quarters=[]))
    assert a.strategy == SKIP
    assert a.quarters_analyzed == 0


def test_crush_consistency_partial() -> None:
    # 3 of 6 quarters crush > 10% → consistency 0.5 (below 0.6 threshold → not crush).
    hist = _hist("PART", [(20, 8, 2), (5, 8, 3), (18, 8, -2), (4, 8, 1), (16, 8, 2), (3, 8, -1)])
    a = IVHistoryAnalyzer().analyze("PART", hist, iv_rank=70, iv_percentile=70)
    assert a.iv_crush_consistency == 0.5
    assert a.strategy == SKIP  # consistency below threshold


def test_analyze_from_event_summary_path() -> None:
    event = EarningsEvent(
        ticker="CSV",
        earnings_date=__import__("datetime").date(2026, 7, 1),
        iv_rank=72,
        iv_percentile=70,
        hist_iv_crush=22.0,
        last_iv_crush=25.0,
        expected_move=8.0,
        last_earnings_move=4.0,  # inside expected → no breach
    )
    a = IVHistoryAnalyzer().analyze_from_event(event)
    assert a.quarters_analyzed == 0
    assert a.avg_iv_crush == 22.0
    assert a.breach_rate == 0.0
    assert a.strategy == IV_CRUSH


def test_lite_path_crush_on_iv_rank_only() -> None:
    # Moomoo enrichment: IV rank present, but NO crush history (hist_iv_crush=0).
    event = EarningsEvent(
        ticker="AAPL",
        earnings_date=__import__("datetime").date(2026, 6, 20),
        iv_rank=72,
        iv_percentile=80,
        hist_iv_crush=0.0,          # no per-quarter crush table
        expected_move=6.5,
    )
    a = IVHistoryAnalyzer().analyze_from_event(event)
    assert a.strategy == IV_CRUSH
    assert 0 < a.confidence <= 70                 # capped lite confidence
    assert "no per-quarter crush history" in a.reasoning


def test_lite_path_spike_on_low_iv_rank() -> None:
    event = EarningsEvent(
        ticker="TSLA",
        earnings_date=__import__("datetime").date(2026, 6, 20),
        iv_rank=22,
        iv_percentile=30,
        hist_iv_crush=0.0,
        expected_move=10.0,
    )
    a = IVHistoryAnalyzer().analyze_from_event(event)
    assert a.strategy == IV_SPIKE
    assert 0 < a.confidence <= 70


def test_lite_path_skip_midrange_iv_rank() -> None:
    event = EarningsEvent(
        ticker="X",
        earnings_date=__import__("datetime").date(2026, 6, 20),
        iv_rank=42,
        iv_percentile=45,
        hist_iv_crush=0.0,
        expected_move=5.0,
    )
    a = IVHistoryAnalyzer().analyze_from_event(event)
    assert a.strategy == SKIP


def test_full_history_takes_precedence_over_lite() -> None:
    # With real crush history, the full (higher-confidence) path is used.
    hist = _hist("NVDA", [(25, 8, 4), (28, 8, -3), (22, 8, 5), (26, 8, 2), (24, 8, -4), (27, 8, 3)])
    a = IVHistoryAnalyzer().analyze("NVDA", hist, iv_rank=70, iv_percentile=70)
    assert a.strategy == IV_CRUSH
    assert a.confidence > 70                       # full path beats the lite cap


def test_analyze_from_event_breach_flag() -> None:
    event = EarningsEvent(
        ticker="BIG",
        earnings_date=__import__("datetime").date(2026, 7, 1),
        iv_rank=20,
        iv_percentile=30,
        hist_iv_crush=8.0,
        expected_move=6.0,
        last_earnings_move=-12.0,  # breach to downside
    )
    a = IVHistoryAnalyzer().analyze_from_event(event)
    assert a.breach_rate == 1.0
    assert a.breach_rate_lower == 1.0
    assert a.avg_actual_move == 12.0
