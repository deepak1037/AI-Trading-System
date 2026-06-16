"""Performance page — P&L attribution by bucket, strategy, and trade history."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from broker_client.analytics.performance_attribution import PerformanceAttribution
from broker_client.analytics.signal_accuracy import SignalAccuracyTracker
from config.settings import settings

st.set_page_config(page_title="Attribution", layout="wide")
st.title("📈 Performance Attribution")

# Period selector
period = st.selectbox("Period", ["MTD", "QTD", "YTD", "30D"], index=0)

attr = PerformanceAttribution()

@st.cache_data(ttl=settings.DASHBOARD_REFRESH_SECONDS)
def get_report(p: str):
    return PerformanceAttribution().generate_report(p)


report = get_report(period)

if report.insufficient_data:
    st.info(
        f"📊 Insufficient data — need at least {settings.MIN_TRADES_FOR_ATTRIBUTION} "
        "closed trades to display attribution."
    )
else:
    # ── P&L Summary ──────────────────────────────────────────────────────────
    st.subheader("P&L Summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Return", f"{report.total_return_pct:+.2f}%")
    col2.metric("vs SPY", f"{report.vs_spy:+.2f}%",
                delta_color="normal" if report.vs_spy >= 0 else "inverse")
    col3.metric("Win Rate", f"{report.win_rate:.0f}%")
    col4.metric("Profit Factor", f"{report.profit_factor:.2f}")

    col5, col6, col7 = st.columns(3)
    col5.metric("Total P&L", f"${report.total_pnl:+,.2f}")
    col6.metric("Avg Winner", f"{report.avg_winner_pct:+.2f}%")
    col7.metric("Avg Loser", f"{report.avg_loser_pct:+.2f}%")

    st.divider()

    # ── Bucket Attribution ────────────────────────────────────────────────────
    st.subheader("By Bucket")
    bkt_names = {1: "Bucket 1 (MSP)", 2: "Bucket 2 (Earnings)", 3: "Bucket 3 (Events)"}
    bkt_rows = []
    for b, attr_b in report.bucket_attribution.items():
        bkt_rows.append({
            "Bucket": bkt_names.get(b, f"B{b}"),
            "Realized P&L": f"${attr_b.realized_pnl:+,.2f}",
            "Trades": attr_b.total_trades,
            "Win Rate": f"{attr_b.win_rate:.0f}%",
            "Avg Hold (days)": f"{attr_b.avg_hold_days:.1f}",
            "Premium Collected": f"${attr_b.premium_collected:,.2f}",
        })
    st.dataframe(pd.DataFrame(bkt_rows), use_container_width=True, hide_index=True)

    bkt_chart_data = {
        "Bucket": [bkt_names.get(b, f"B{b}") for b in report.bucket_attribution],
        "P&L ($)": [a.realized_pnl for a in report.bucket_attribution.values()],
    }
    fig_b = px.bar(
        pd.DataFrame(bkt_chart_data),
        x="Bucket", y="P&L ($)",
        color="P&L ($)",
        color_continuous_scale="RdYlGn",
        title="Bucket P&L",
    )
    st.plotly_chart(fig_b, use_container_width=True)

    st.divider()

    # ── Strategy Attribution ──────────────────────────────────────────────────
    if report.strategy_attribution:
        st.subheader("By Strategy")
        strat_rows = []
        for s, sa in report.strategy_attribution.items():
            strat_rows.append({
                "Strategy": s,
                "Realized P&L": f"${sa.realized_pnl:+,.2f}",
                "Trades": sa.trade_count,
                "Win Rate": f"{sa.win_rate:.0f}%",
                "Avg Return": f"{sa.avg_return_pct:+.2f}%",
                "Avg Hold (days)": f"{sa.avg_hold_days:.1f}",
            })
        st.dataframe(pd.DataFrame(strat_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Best / Worst Trades ───────────────────────────────────────────────────
    st.subheader("Best / Worst Trades")
    col_best, col_worst = st.columns(2)
    if report.best_trade:
        with col_best:
            bt = report.best_trade
            st.success(
                f"🏆 **Best:** {bt.ticker} ({bt.strategy})\n\n"
                f"P&L: ${bt.realized_pnl:+,.2f}"
            )
    if report.worst_trade:
        with col_worst:
            wt = report.worst_trade
            st.error(
                f"📉 **Worst:** {wt.ticker} ({wt.strategy})\n\n"
                f"P&L: ${wt.realized_pnl:+,.2f}"
            )

# ── Signal Accuracy ───────────────────────────────────────────────────────────
st.divider()
st.subheader("🎯 Signal Accuracy")

@st.cache_data(ttl=settings.DASHBOARD_REFRESH_SECONDS)
def get_accuracy(p: str):
    return SignalAccuracyTracker().generate_report(p)


acc = get_accuracy(period)
if acc.insufficient_data:
    st.info(
        f"Need at least {settings.MIN_SIGNALS_FOR_ACCURACY} tracked signals to show accuracy."
    )
else:
    col_a1, col_a2, col_a3 = st.columns(3)
    col_a1.metric("High Confidence Accuracy", f"{acc.high_confidence_accuracy * 100:.1f}%")
    col_a2.metric("Critical Accuracy (>80)", f"{acc.critical_accuracy * 100:.1f}%")
    col_a3.metric("Optimal Threshold", acc.optimal_confidence_threshold)

    col_d1, col_d2, col_d3 = st.columns(3)
    col_d1.metric("Long Accuracy", f"{acc.long_accuracy * 100:.1f}%")
    col_d2.metric("Short Accuracy", f"{acc.short_accuracy * 100:.1f}%")
    col_d3.metric("Neutral Accuracy", f"{acc.neutral_accuracy * 100:.1f}%")

    if acc.source_accuracy:
        st.write("**Per-source accuracy:**")
        src_rows = [
            {"Source": s, "Accuracy": f"{v * 100:.1f}%"}
            for s, v in sorted(acc.source_accuracy.items(), key=lambda x: -x[1])
        ]
        st.dataframe(pd.DataFrame(src_rows), use_container_width=True, hide_index=True)
