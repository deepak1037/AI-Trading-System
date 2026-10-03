"""
dashboard/pages/12_strategy_health.py

Strategy Health dashboard page.
Shows per-strategy:
  - Expectancy (Win% × AvgWin − Loss% × AvgLoss)
  - Current regime gate status
  - Trade count over rolling 90-day window
  - Profit factor

Add to dashboard by dropping this file in dashboard/pages/
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="Strategy Health", page_icon="📊", layout="wide")
st.title("📊 Strategy Health")
st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}  |  90-day rolling window")

DB_PATH = os.getenv("DB_PATH", "trading.db")

# ── load expectancy ────────────────────────────────────────────────────────────
try:
    from broker_client.analytics.expectancy_tracker import ExpectancyTracker
    tracker = ExpectancyTracker(db_path=DB_PATH)
    exp_map = tracker.report()
    exp_df = pd.DataFrame([
        {
            "Strategy":       e.strategy,
            "Trades (90d)":   e.n_trades,
            "Win Rate":       f"{e.win_rate:.0%}" if e.n_trades else "—",
            "Avg Win %":      f"{e.avg_win_pct:.2f}%" if e.n_trades else "—",
            "Avg Loss %":     f"{e.avg_loss_pct:.2f}%" if e.n_trades else "—",
            "Expectancy %":   f"{e.expectancy:.2f}%" if e.n_trades else "—",
            "Profit Factor":  f"{e.profit_factor:.2f}" if e.profit_factor else "—",
            "Grade":          e.grade,
            "_exp_raw":       e.expectancy,
            "_n":             e.n_trades,
        }
        for e in exp_map.values()
    ]).sort_values("_exp_raw", ascending=False).reset_index(drop=True)
    exp_ok = True
except Exception as ex:
    st.warning(f"Could not load expectancy data: {ex}")
    exp_df = pd.DataFrame()
    exp_ok = False

# ── load regime gate ───────────────────────────────────────────────────────────
try:
    from broker_client.risk.strategy_regime_filter import StrategyRegimeFilter
    gate = StrategyRegimeFilter(db_path=DB_PATH)
    regime = gate.regime
    regime_color = {"bull": "🟢", "neutral": "🟡", "bear": "🔴", "unknown": "⚪"}.get(
        regime.value, "⚪"
    )
    regime_ok = True
except Exception as ex:
    st.warning(f"Could not load regime filter: {ex}")
    regime = None
    regime_color = "⚪"
    regime_ok = False

# ── top metric row ─────────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)

if regime_ok:
    col1.metric("Market Regime", f"{regime_color} {regime.value.upper()}")
else:
    col1.metric("Market Regime", "Unknown")

if exp_ok and not exp_df.empty:
    traded = exp_df[exp_df["_n"] > 0]
    positive = (traded["_exp_raw"] > 0).sum()
    col2.metric("Positive Expectancy", f"{positive}/{len(traded)} strategies")
    best = traded.iloc[0] if len(traded) else None
    if best is not None:
        col3.metric(
            "Best Strategy",
            best["Strategy"],
            delta=f"E={best['_exp_raw']:.2f}%",
        )
    worst = traded.iloc[-1] if len(traded) else None
    if worst is not None:
        col4.metric(
            "Worst Strategy",
            worst["Strategy"],
            delta=f"E={worst['_exp_raw']:.2f}%",
            delta_color="inverse",
        )

st.divider()

# ── expectancy table ───────────────────────────────────────────────────────────
st.subheader("Per-Strategy Expectancy (90-day window)")
if exp_ok and not exp_df.empty:
    display_cols = [
        "Strategy", "Trades (90d)", "Win Rate",
        "Avg Win %", "Avg Loss %", "Expectancy %", "Profit Factor", "Grade"
    ]
    # Color code the expectancy column
    def color_exp(val):
        if val == "—":
            return "color: gray"
        try:
            v = float(val.replace("%", ""))
            if v >= 1.0:
                return "color: #00C851"   # green
            elif v >= 0:
                return "color: #ffbb33"   # amber
            else:
                return "color: #ff4444"   # red
        except:
            return ""

    styled = exp_df[display_cols].style.applymap(color_exp, subset=["Expectancy %"])
    st.dataframe(styled, use_container_width=True, hide_index=True)
else:
    st.info("No trade history yet. Close some trades to see expectancy data.")

st.divider()

# ── regime gate status ─────────────────────────────────────────────────────────
st.subheader("Strategy Regime Gates")
if regime_ok:
    st.code(gate.explain(), language=None)
else:
    st.info("Regime filter not available.")

st.divider()

# ── mini bar chart ─────────────────────────────────────────────────────────────
if exp_ok and not exp_df.empty:
    traded = exp_df[exp_df["_n"] > 0].copy()
    if not traded.empty:
        st.subheader("Expectancy by Strategy")
        bar_data = traded.set_index("Strategy")["_exp_raw"]
        st.bar_chart(bar_data, use_container_width=True)

st.divider()
st.caption(
    "Expectancy = Win% × Avg Win% − Loss% × Avg Loss%  |  "
    "Positive expectancy = edge.  Negative = stop trading that strategy."
)

# Auto-refresh
if st.button("🔄 Refresh"):
    st.rerun()
