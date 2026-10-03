"""
discord_alerts/discord_report.py

Performance report for paper trades sourced from Discord signals.

Usage:
    python discord_alerts/discord_report.py [db/trading.db]
    python discord_alerts/discord_report.py db/trading.db --days 90
    python discord_alerts/discord_report.py db/trading.db --close 42 3.15
"""

from __future__ import annotations

import sys
import sqlite3
from datetime import datetime, timezone, timedelta


def auto_expire(db_path: str) -> int:
    """Mark trades whose expiry_short has passed as expired (paper loss = 100%)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, paper_entry FROM discord_signals
               WHERE paper_status = 'open'
               AND expiry_short IS NOT NULL
               AND expiry_short < ?""", (today,)
        ).fetchall()
        for row_id, entry in rows:
            conn.execute(
                """UPDATE discord_signals
                   SET paper_status='expired', paper_exit=0,
                       paper_pnl_pct=-100.0,
                       closed_at=?, skip_reason='expired'
                   WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), row_id)
            )
    return len(rows)


def fetch_signals(db_path: str, days: int = 0) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            if days > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                rows = conn.execute(
                    "SELECT * FROM discord_signals WHERE received_at >= ? ORDER BY received_at DESC",
                    (cutoff,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM discord_signals ORDER BY received_at DESC"
                ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def print_report(db_path: str = "db/trading.db", days: int = 0) -> None:
    expired_n = auto_expire(db_path)
    if expired_n:
        print(f"  ⏰ Auto-expired {expired_n} trade(s) past expiry date")

    signals = fetch_signals(db_path, days)
    if not signals:
        print("No signals found.")
        return

    period   = f"Last {days} days" if days else "All time"
    total    = len(signals)
    tradeable = [s for s in signals if s["parse_confidence"] in ("high", "medium")]
    skipped  = [s for s in signals if s["parse_confidence"] == "low"]
    open_    = [s for s in tradeable if s["paper_status"] == "open"]
    closed   = [s for s in tradeable if s["paper_status"] == "closed"]
    expired  = [s for s in tradeable if s["paper_status"] == "expired"]
    all_done = closed + expired

    wins     = [s for s in all_done if (s["paper_pnl_pct"] or 0) > 0]
    losses   = [s for s in all_done if (s["paper_pnl_pct"] or 0) <= 0]
    win_rate = len(wins) / len(all_done) * 100 if all_done else 0
    avg_win  = sum(s["paper_pnl_pct"] for s in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(s["paper_pnl_pct"] for s in losses) / len(losses) if losses else 0
    expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * abs(avg_loss))

    print(f"\n{'='*70}")
    print(f"  OptionsKit Discord Signal Report — {period}")
    print(f"{'='*70}")
    print(f"  Total messages processed : {total}")
    print(f"  Tradeable signals        : {len(tradeable)}")
    print(f"  Skipped (unparseable)    : {len(skipped)}")
    print(f"  Currently open           : {len(open_)}")
    print(f"  Manually closed          : {len(closed)}")
    print(f"  Expired                  : {len(expired)}")
    if all_done:
        print(f"\n  Win Rate                 : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Avg Win                  : +{avg_win:.2f}%")
        print(f"  Avg Loss                 : {avg_loss:.2f}%")
        print(f"  Expectancy               : {expectancy:.2f}%")

    if open_:
        print(f"\n  {'─'*66}")
        print(f"  OPEN POSITIONS ({len(open_)})")
        print(f"  {'─'*66}")
        print(f"  {'ID':>4}  {'Signal Date':>12}  {'Act':<4}  {'Symbol':<6}  {'Strike':>8}  {'Entry':>6}  Expiry")
        print(f"  {'─'*66}")
        for s in open_:
            date = s["received_at"][:10] if s["received_at"] else "?"
            print(f"  {s['id']:>4}  {date:>12}  {s['action'] or '?':>4}  "
                  f"{s['symbol'] or '?':6}  {s['strike'] or 0:>8.2f}  "
                  f"{s['paper_entry'] or 0:>6.2f}  {s['expiry_short'] or '?'}")

    if all_done:
        print(f"\n  {'─'*66}")
        print(f"  CLOSED / EXPIRED ({len(all_done)})")
        print(f"  {'─'*66}")
        print(f"  {'ID':>4}  {'Date':>10}  {'Symbol':<6}  {'Entry':>6}  {'Exit':>6}  {'P&L%':>7}  Result")
        print(f"  {'─'*66}")
        for s in sorted(all_done, key=lambda x: x["closed_at"] or ""):
            date = s["received_at"][:10] if s["received_at"] else "?"
            pnl  = s["paper_pnl_pct"] or 0
            status = s["paper_status"]
            icon = "✅" if pnl > 0 else ("⏰" if status == "expired" else "❌")
            print(f"  {s['id']:>4}  {date:>10}  {s['symbol'] or '?':6}  "
                  f"{s['paper_entry'] or 0:>6.2f}  {s['paper_exit'] or 0:>6.2f}  "
                  f"{pnl:>+7.2f}%  {icon} {status}")

    if all_done:
        by_type: dict[str, list[float]] = {}
        for s in all_done:
            t = s["strategy_type"] or "unknown"
            by_type.setdefault(t, []).append(s["paper_pnl_pct"] or 0)
        print(f"\n  {'─'*66}")
        print(f"  BY STRATEGY")
        print(f"  {'─'*66}")
        print(f"  {'Strategy':<20}  {'N':>4}  {'WinRate':>8}  {'AvgP&L':>8}  {'Expect':>8}")
        for stype, pnls in sorted(by_type.items()):
            w = [p for p in pnls if p > 0]
            l = [p for p in pnls if p <= 0]
            wr  = len(w)/len(pnls)*100
            avg = sum(pnls)/len(pnls)
            aw  = sum(w)/len(w) if w else 0
            al  = abs(sum(l)/len(l)) if l else 0
            exp = wr/100*aw - (1-wr/100)*al
            print(f"  {stype:<20}  {len(pnls):>4}  {wr:>7.1f}%  {avg:>+7.2f}%  {exp:>+7.2f}%")

    print(f"\n{'='*70}")
    print(f"\nTo close a trade:  python discord_alerts/discord_report.py {db_path} --close <ID> <EXIT_PRICE>")
    print()


def close_trade(db_path: str, signal_id: int, exit_price: float) -> None:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from discord_alerts.discord_signal_reader import SignalDB
    db = SignalDB(db_path)
    db.close_signal(signal_id, exit_price, reason="manual_close")
    print(f"✅ Trade #{signal_id} closed at {exit_price}")
    print_report(db_path)


if __name__ == "__main__":
    db   = sys.argv[1] if len(sys.argv) > 1 else "db/trading.db"
    days = 0
    if "--days" in sys.argv:
        idx  = sys.argv.index("--days")
        days = int(sys.argv[idx+1])
    if "--close" in sys.argv:
        idx = sys.argv.index("--close")
        close_trade(db, int(sys.argv[idx+1]), float(sys.argv[idx+2]))
    else:
        print_report(db, days)
