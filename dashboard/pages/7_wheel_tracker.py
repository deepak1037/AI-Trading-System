"""Wheel Tracker page — active wheel cycles, premium collected, next action (Step 10)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from broker_client.buckets.wheel_tracker import WheelTracker
from config.settings import settings

st.set_page_config(page_title="Wheel Tracker", layout="wide")
st.title("🎡 Wheel Strategy Tracker")

# Next-action hint per phase.
_NEXT_ACTION = {
    "sell_put": "Awaiting expiry/assignment — hold the cash-secured put",
    "assigned": "Shares assigned — sell a covered call to start the call leg",
    "sell_call": "Awaiting expiry/called-away — hold the covered call",
    "called_away": "Cycle complete — restart the wheel with a new put",
    "restart": "Restart the wheel with a new put",
}


def _tracker() -> WheelTracker:
    return WheelTracker(db_path=settings.DB_PATH)


tracker = _tracker()
wheels = tracker.get_all_wheels()

if not wheels:
    st.info("No wheel cycles recorded yet. They appear here once a put is sold.")
    st.stop()

# ── Portfolio-wide wheel totals ───────────────────────────────────────────────
total_premium = sum(w.total_premium_collected for w in wheels)
col1, col2, col3 = st.columns(3)
col1.metric("Tickers on the wheel", len(wheels))
col2.metric("Total premium collected", f"${total_premium:,.0f}")
col3.metric(
    "Avg cycle return",
    f"{(sum(w.avg_cycle_return_pct for w in wheels) / len(wheels)):+.1f}%",
)

st.divider()

# ── Per-ticker detail ─────────────────────────────────────────────────────────
for summary in wheels:
    st.subheader(f"{summary.ticker} — cycle #{summary.total_cycles} ({summary.current_phase})")
    c1, c2, c3 = st.columns(3)
    c1.metric("Premium collected", f"${summary.total_premium_collected:,.0f}")
    c2.metric("Avg cycle return", f"{summary.avg_cycle_return_pct:+.1f}%")
    c3.metric("Avg annualized", f"{summary.avg_annualized_return_pct:+.1f}%")
    st.caption(f"➡️ Next: {_NEXT_ACTION.get(summary.current_phase, 'Monitor')}")

    cycles = tracker.get_cycles(summary.ticker)
    rows = [
        {
            "Cycle": c.cycle_number,
            "Phase": c.current_phase,
            "Put Strike": c.put_strike,
            "Call Strike": c.call_strike,
            "Cost Basis": c.cost_basis,
            "Premium ($)": round(c.total_premium_collected, 2),
            "Return %": c.cycle_return_pct,
            "Annualized %": c.annualized_return_pct,
        }
        for c in cycles
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    st.divider()

st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s")
