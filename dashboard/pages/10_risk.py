"""Risk Monitor — portfolio heat map, concentration alerts, regime advisor."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from broker_client.risk.concentration_checker import ConcentrationChecker
from broker_client.risk.portfolio_heat_map import PortfolioHeatMap
from broker_client.risk.regime_advisor import RegimeAdvisor
from config.settings import settings
from data.db import get_connection

st.set_page_config(page_title="Risk Monitor", layout="wide")
st.title("🛡️ Risk Monitor")


@st.cache_data(ttl=settings.DASHBOARD_REFRESH_SECONDS)
def load_open_positions() -> list[dict]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker, strategy, bucket, sub_type, option_type, strike, expiry, "
                "qty, entry_price, position_type FROM positions WHERE is_open=1"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


positions = load_open_positions()

# ── Heat map ─────────────────────────────────────────────────────────────────
st.subheader("📊 Portfolio Heat Map")

hm = PortfolioHeatMap()
try:
    report = hm.generate(positions)
except Exception as exc:
    st.error(f"Heat map error: {exc}")
    report = None

if report:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Positions", report.total_positions)
    col2.metric(
        "Net Delta",
        f"{report.net_delta:+.2f}",
        delta=report.delta_direction,
    )
    col3.metric(
        "Cash Buffer",
        f"{report.cash_buffer_pct:.1f}%",
        delta="⚠️ LOW" if report.cash_buffer_warning else "✅ OK",
        delta_color="inverse" if report.cash_buffer_warning else "normal",
    )
    col4.metric(
        f"Max Sector ({report.max_sector_name})",
        f"{report.max_sector_pct:.1f}%",
        delta="⚠️ HIGH" if report.sector_warning else "✅ OK",
        delta_color="inverse" if report.sector_warning else "normal",
    )

    st.divider()

    # Sector pie chart
    if report.sector_exposure:
        col_pie, col_exp = st.columns(2)
        with col_pie:
            st.subheader("Sector Breakdown")
            sec_data = {
                "Sector": list(report.sector_exposure.keys()),
                "Exposure %": [v.exposure_pct for v in report.sector_exposure.values()],
            }
            df_sec = pd.DataFrame(sec_data)
            fig = px.pie(df_sec, names="Sector", values="Exposure %", hole=0.4)
            st.plotly_chart(fig, use_container_width=True)

        with col_exp:
            st.subheader("Sector Table")
            rows_sec = []
            for s, e in report.sector_exposure.items():
                rows_sec.append({
                    "Sector": s,
                    "Exposure %": f"{e.exposure_pct:.1f}%",
                    "Positions": e.positions,
                    "Tickers": ", ".join(e.tickers),
                    "Status": "⚠️ HIGH" if e.warning else "✅",
                })
            st.dataframe(pd.DataFrame(rows_sec), use_container_width=True, hide_index=True)

    # Expiry timeline
    st.subheader("Expiry Concentration")
    if report.expiry_buckets:
        df_exp = pd.DataFrame(
            [{"Window": k, "Positions": v} for k, v in report.expiry_buckets.items()]
        )
        fig2 = px.bar(df_exp, x="Window", y="Positions", color="Positions",
                      color_continuous_scale="RdYlGn_r")
        st.plotly_chart(fig2, use_container_width=True)
        if report.same_expiry_warning:
            st.warning(f"⚠️ {max(report.expiry_buckets.values())} positions in same expiry week — consider staggering")
    else:
        st.info("No expiry data available.")

    # Correlated clusters
    if report.correlated_clusters:
        st.subheader("Correlated Clusters")
        for cl in report.correlated_clusters:
            with st.expander(f"{cl.cluster_name} — {cl.combined_exposure_pct:.1f}% combined"):
                st.write(f"**Tickers:** {', '.join(cl.tickers)}")
                st.write(f"**Risk note:** {cl.risk_note}")

# ── Concentration Alerts ──────────────────────────────────────────────────────
st.divider()
st.subheader("⚠️ Concentration Alerts")

checker = ConcentrationChecker()
try:
    alerts = checker.check(positions)
except Exception as exc:
    alerts = []
    st.error(f"Concentration check error: {exc}")

if alerts:
    for a in alerts:
        icon = "🚨" if a.severity == "CRITICAL" else "⚠️"
        color = "error" if a.severity == "CRITICAL" else "warning"
        getattr(st, color)(f"{icon} **{a.alert_type}**: {a.message}\n\n*{a.suggestion}*")
else:
    st.success("✅ No concentration alerts — portfolio looks healthy.")

# ── Regime Advisor ────────────────────────────────────────────────────────────
st.divider()
st.subheader("🎯 Regime Advisor")

try:
    from watcher.market_state import MarketStateTracker
    from signals.signal_fusion import SignalFusion
    _fusion = SignalFusion()
    _ms = _fusion.current_state
except Exception:
    _ms = None

advisor = RegimeAdvisor()
try:
    advice = advisor.advise(_ms)
    col_r1, col_r2, col_r3 = st.columns(3)
    col_r1.metric("Regime", advice.regime.upper())
    col_r2.metric("OTM Buffer Multiplier", f"{advice.put_otm_buffer_multiplier:.2f}x")
    col_r3.metric("Confidence Score", advice.confidence)

    st.write("**Per-bucket strategy adjustments:**")
    col_b1, col_b2, col_b3 = st.columns(3)
    col_b1.metric("Bucket 1 (MSP)", advice.bucket1_adjustment.upper())
    col_b2.metric("Bucket 2 (Earnings)", advice.bucket2_adjustment.upper())
    col_b3.metric("Bucket 3 (Events)", advice.bucket3_adjustment.upper())

    if advice.guidance:
        st.write("**Guidance:**")
        for g in advice.guidance:
            st.write(f"• {g}")

    if advice.warnings:
        for w in advice.warnings:
            st.warning(w)

except Exception as exc:
    st.info(f"Regime advisor unavailable: {exc}")
