"""CLI for the earnings analyzer (Phase 3, Step 6).

Usage::

    python -m broker_client.earnings.cli                # next 14 days
    python -m broker_client.earnings.cli --days 7       # best plays this week
    python -m broker_client.earnings.cli --ticker NVDA  # one ticker

Wired into the Makefile as ``make earnings`` / ``earnings-ticker`` / ``earnings-week``.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from broker_client.earnings.earnings_scanner import EarningsScanner  # noqa: E402
from broker_client.earnings.models import (  # noqa: E402
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsOpportunity,
    IVCrushTrade,
    IVSpikeTrade,
)

_BAR = "=" * 70
_RULE = "─" * 70


def render(opportunities: list[EarningsOpportunity], days: int) -> str:
    actionable = [o for o in opportunities if o.strategy != SKIP]
    lines = [
        "",
        _BAR,
        f"  EARNINGS OPPORTUNITIES — Next {days} Days",
        f"  {len(actionable)} plays found ({len(opportunities)} scanned)",
        _BAR,
    ]
    if not actionable:
        lines.append("\n  No actionable earnings plays in the window.\n")
        lines.append(_BAR)
        return "\n".join(lines)

    for opp in actionable:
        a = opp.iv_analysis
        lines.append("")
        lines.append(_RULE)
        lines.append(f"  {opp.ticker} — {opp.earnings_date} {opp.earnings_time}")
        lines.append(f"  Strategy: {opp.strategy} | Score: {opp.overall_score}/100 "
                     f"| Priority: {opp.priority}")
        lines.append(f"  IV Rank: {a.iv_rank_current}/100  "
                     f"Expected Move: ±{a.expected_move_current:.1f}%")
        lines.append(f"  Avg IV Crush: {a.avg_iv_crush:.1f}%  Breach Rate: {a.breach_rate:.0%}")
        lines.extend(_render_trade(opp))
        if opp.llm_assessment is not None:
            la = opp.llm_assessment
            lines.append("")
            lines.append(f"  {la.model.upper()}: {la.recommendation} "
                         f"({la.confidence}% confidence, PoP {la.probability_of_profit}%)")
            if la.reasoning:
                lines.append(f"  \"{la.reasoning}\"")
    lines.append("")
    lines.append(_BAR)
    return "\n".join(lines)


def _render_trade(opp: EarningsOpportunity) -> list[str]:
    trade = opp.trade
    if trade is None:
        return []
    out = ["", "  TRADE:"]
    if opp.strategy == IV_CRUSH and isinstance(trade, IVCrushTrade):
        out.append(f"    Sell {trade.put_strike:g}P @ ${trade.put_premium:.2f} "
                   f"(exp {trade.put_expiry})")
        out.append(f"    Margin: ${trade.margin_required:,.0f} ({trade.margin_basis})")
        out.append(f"    ROI (margin): {trade.roi_margin:.1%}")
        out.append(f"    Break-even: {trade.break_even_pct:.1f}% drop needed to lose")
    elif opp.strategy == IV_SPIKE and isinstance(trade, IVSpikeTrade):
        out.append(f"    Buy {trade.instrument}: {trade.call_strike:g}C/{trade.put_strike:g}P")
        out.append(f"    Cost: ${trade.total_premium_paid:,.0f}  (max loss = cost)")
        out.append(f"    ENTER {trade.entry_date} → EXIT {trade.exit_date} "
                   f"(1 day BEFORE earnings)")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Earnings IV opportunity scanner")
    parser.add_argument("--days", type=int, default=None, help="Days ahead to scan (default 14)")
    parser.add_argument("--ticker", default=None, help="Scan a single ticker")
    parser.add_argument("--save", action="store_true", help="Persist results to the DB")
    args = parser.parse_args(argv)

    try:
        from broker_core.factory import get_broker

        broker = get_broker()
    except Exception as exc:  # noqa: BLE001 — scanner degrades to estimates
        print(f"[warn] broker unavailable, using estimates: {exc}", file=sys.stderr)
        broker = None

    scanner = EarningsScanner(broker=broker)
    from config.settings import settings

    days = args.days if args.days is not None else settings.EARNINGS_DAYS_AHEAD

    if args.ticker:
        opp = scanner.scan_ticker(args.ticker, days_ahead=days)
        opportunities = [opp] if opp is not None else []
    else:
        opportunities = scanner.scan(days_ahead=days)

    if args.save and opportunities:
        scanner.save(opportunities)

    print(render(opportunities, days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
