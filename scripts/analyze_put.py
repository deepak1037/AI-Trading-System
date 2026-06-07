"""CLI: LLM-augmented put-selling ROI analysis.

Usage:
    python scripts/analyze_put.py --ticker HOOD --strike 8 --expiry 2026-07-18
    make analyze ticker=HOOD strike=8 expiry=2026-07-18
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure project root is importable.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from broker_client.llm_roi_analyzer import LLMROIAnalysis, LLMROIAnalyzer  # noqa: E402
from core.exceptions import DataError  # noqa: E402

_BAR = "━" * 48

_REC_BADGE = {
    "STRONG_SELL": "STRONG SELL ✅",
    "SELL": "SELL 🟢",
    "NEUTRAL": "NEUTRAL 🟡",
    "AVOID": "AVOID 🔴",
}


def _fmt_expiry_label(expiry: str) -> str:
    """2026-07-18 → Jul-18."""
    try:
        from datetime import date

        d = date.fromisoformat(expiry)
        return d.strftime("%b-%d")
    except Exception:  # noqa: BLE001
        return expiry


def render(analysis: LLMROIAnalysis) -> str:
    roi = analysis.roi
    a = analysis.assessment
    badge = _REC_BADGE.get(a.recommendation, a.recommendation)
    exp_label = _fmt_expiry_label(roi.expiry)

    lines: list[str] = []
    lines.append(_BAR)
    lines.append(f"PUT OPPORTUNITY ANALYSIS — {roi.ticker} ${roi.strike:g} PUT {exp_label}")
    lines.append(_BAR)
    lines.append("QUANTITATIVE")
    lines.append(
        f"  Net premium/contract: ${roi.premium_per_contract:,.0f}  "
        f"(gross ${roi.gross_premium_per_contract:,.0f} − ${roi.commission_per_contract:.2f} comm)"
    )
    lines.append(f"  Margin/contract:     ${roi.margin_per_contract:,.0f}  ({roi.margin_basis})")
    lines.append(f"  Static monthly ROI:  {roi.monthly_roi_pct:.1f}%  (margin basis)")
    lines.append(
        f"  Cash-secured monthly: {roi.cash_secured_monthly_roi_pct:.1f}%  "
        f"(${roi.cash_secured_margin_per_contract:,.0f} capital)"
    )
    if roi.delta is not None:
        lines.append(f"  Delta:               {roi.delta:.2f}  (≈ assignment prob)")
    lines.append("")
    lines.append(f"LLM ASSESSMENT                    Confidence: {a.confidence}%")
    lines.append(f"  Recommendation:      {badge}")
    lines.append(f"  Expires worthless:   {a.expiry_worthless_probability}% probability")
    lines.append(
        f"  Expected 50% decay:  {a.expected_50pct_decay_days} days "
        f"(static assumes {roi.days_to_expiry} days)"
    )
    lines.append(f"  Adjusted monthly ROI: {a.adjusted_monthly_roi_pct:.1f}%  ← KEY NUMBER")
    if a.bounce_scenario:
        lines.append("")
        lines.append("  Bounce scenario:")
        lines.append(f'  "{a.bounce_scenario}"')
    if a.assignment_scenario:
        lines.append("")
        lines.append("  Assignment scenario:")
        lines.append(f'  "{a.assignment_scenario}"')
    if a.key_risks:
        lines.append("")
        lines.append("  Key risks:")
        for risk in a.key_risks:
            lines.append(f"  • {risk}")
    if a.reasoning:
        lines.append("")
        lines.append("  Reasoning:")
        lines.append(f"  {a.reasoning}")
    lines.append("")
    lines.append(_BAR)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM put-selling ROI analysis")
    parser.add_argument("--ticker", required=True, help="Underlying symbol, e.g. HOOD")
    parser.add_argument("--strike", required=True, type=float, help="Put strike, e.g. 8")
    parser.add_argument("--expiry", required=True, help="Expiry YYYY-MM-DD")
    args = parser.parse_args(argv)

    try:
        from broker_core.factory import get_broker

        broker = get_broker()
    except Exception as exc:  # noqa: BLE001 — analysis can fall back to yfinance
        print(f"[warn] broker unavailable, using yfinance only: {exc}", file=sys.stderr)
        broker = None

    analyzer = LLMROIAnalyzer(broker=broker)
    try:
        analysis = analyzer.analyze(args.ticker, args.strike, args.expiry)
    except DataError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1

    print()
    print(render(analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
