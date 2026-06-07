"""Options analyzer page — LLM-augmented put-selling ROI (Day 10+).

Shows the quantitative PutROIResult for a strike across all expiries, then a
Claude assessment (probability-adjusted ROI + STRONG_SELL..AVOID badge) for the
selected expiry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Options Analyzer", layout="wide")
st.title("Put-Selling ROI Analyzer")
st.caption(
    "Quantitative ROI across expiries + a Claude assessment of the "
    "probability-adjusted return and a STRONG_SELL..AVOID recommendation."
)

# Recommendation → background colour for the badge.
_REC_COLORS = {
    "STRONG_SELL": "#1b7837",  # green
    "SELL": "#7fbf7b",         # light green
    "NEUTRAL": "#e6c200",      # yellow
    "AVOID": "#c0392b",        # red
}
_REC_LABEL = {
    "STRONG_SELL": "STRONG SELL",
    "SELL": "SELL",
    "NEUTRAL": "NEUTRAL",
    "AVOID": "AVOID",
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_broker():
    try:
        from broker_core.factory import get_broker
        return get_broker()
    except Exception as exc:  # noqa: BLE001 — analyzer falls back to yfinance
        st.warning(f"Broker unavailable, using yfinance for prices: {exc}")
        return None


def _roi_row(roi) -> dict:
    return {
        "Expiry": roi.expiry,
        "DTE": roi.days_to_expiry,
        "Bid": f"${roi.bid:.2f}",
        "Ask": f"${roi.ask:.2f}",
        "Delta": f"{roi.delta:.2f}" if roi.delta is not None else "—",
        "Net premium": f"${roi.premium_per_contract:,.0f}",
        "Margin": f"${roi.margin_per_contract:,.0f}",
        "Basis": roi.margin_basis,
        "OTM %": f"{roi.otm_pct:.1f}%",
        "Monthly ROI (margin)": f"{roi.monthly_roi_pct:.1f}%",
        "Monthly ROI (cash-sec)": f"{roi.cash_secured_monthly_roi_pct:.1f}%",
        "Annualized (margin)": f"{roi.annualized_roi_pct:.1f}%",
    }


# ── Inputs ────────────────────────────────────────────────────────────────────
col1, col2, col3 = st.columns(3)
with col1:
    ticker = st.text_input("Ticker", value="", placeholder="HOOD").upper().strip()
with col2:
    strike = st.number_input("Strike", min_value=0.5, value=8.0, step=0.5, format="%.1f")
with col3:
    expiry = st.text_input(
        "Expiry (YYYY-MM-DD, blank = nearest)", value="", placeholder="2026-07-18"
    ).strip()

run = st.button("Run Full Analysis", type="primary", use_container_width=True)

if not settings.ANTHROPIC_API_KEY:
    st.info(
        "ANTHROPIC_API_KEY is not set — the ROI table will still render, but the "
        "Claude assessment will be skipped. Add the key to `.env` to enable it."
    )


# ── Run ───────────────────────────────────────────────────────────────────────
if run:
    if not ticker:
        st.error("Enter a ticker.")
        st.stop()

    from broker_client.llm_roi_analyzer import LLMROIAnalyzer
    from core.exceptions import DataError

    broker = _get_broker()
    analyzer = LLMROIAnalyzer(broker=broker)

    # ── ROI table across all expiries ─────────────────────────────────────────
    st.subheader(f"ROI across expiries — {ticker} ${strike:g} put")

    rows: list[dict] = []
    with st.spinner("Fetching chain and computing ROI across expiries..."):
        # Fast path: one broker chain fetch → ROI for every expiry in memory.
        results = []
        try:
            results = analyzer.build_roi_table(ticker, float(strike))
        except Exception as exc:  # noqa: BLE001 — fall back below
            st.caption(f"Chain fetch note: {exc}")

        if results:
            rows = [_roi_row(r) for r in results]
        else:
            # Fallback (no broker): list expiries + per-expiry compute via yfinance.
            expiries = analyzer.list_expiries(ticker)
            if expiry and expiry not in expiries:
                expiries = [expiry] + expiries
            if not expiries:
                st.warning(
                    f"Could not list expiries for {ticker} (broker unavailable and "
                    "yfinance rate-limited). Try again shortly."
                )
                st.stop()
            for exp in expiries:
                try:
                    rows.append(_roi_row(analyzer.build_put_roi(ticker, float(strike), exp)))
                except Exception as exc:  # noqa: BLE001 — skip bad expiries
                    rows.append({"Expiry": exp, "DTE": "—", "Net premium": f"err: {exc}"})

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            "Table margin uses the Reg-T estimate for speed; the Claude analysis "
            "below uses Schwab's real buying-power margin for the chosen expiry."
        )
    else:
        st.warning("No ROI rows could be computed (no options data for this strike).")
        st.stop()

    # ── LLM assessment for the chosen expiry ──────────────────────────────────
    # Use the typed expiry, else the soonest in the table (rows are sorted).
    target_expiry = expiry or (rows[0].get("Expiry", "") if rows else "")
    if not target_expiry:
        st.stop()

    st.divider()
    st.subheader(f"Claude assessment — {ticker} ${strike:g} put {target_expiry}")

    if not settings.ANTHROPIC_API_KEY:
        st.warning("Skipped — ANTHROPIC_API_KEY not configured.")
        st.stop()

    with st.spinner(f"Asking {settings.LLM_MODEL}..."):
        try:
            analysis = analyzer.analyze(ticker, float(strike), target_expiry)
        except DataError as exc:
            st.error(f"Analysis failed: {exc}")
            st.stop()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Unexpected error: {exc}")
            st.stop()

    a = analysis.assessment
    color = _REC_COLORS.get(a.recommendation, "#666")
    label = _REC_LABEL.get(a.recommendation, a.recommendation)

    # Recommendation badge.
    st.markdown(
        f"""<div style="background-color:{color};color:white;padding:14px 20px;
        border-radius:8px;font-size:22px;font-weight:700;display:inline-block;">
        {label} &nbsp;·&nbsp; confidence {a.confidence}%</div>""",
        unsafe_allow_html=True,
    )
    st.write("")

    m1, m2, m3 = st.columns(3)
    m1.metric("Expires worthless", f"{a.expiry_worthless_probability}%")
    m2.metric(
        "Expected 50% decay",
        f"{a.expected_50pct_decay_days} d",
        delta=f"vs {analysis.roi.days_to_expiry} DTE static",
        delta_color="off",
    )
    m3.metric(
        "Adjusted monthly ROI",
        f"{a.adjusted_monthly_roi_pct:.1f}%",
        delta=f"static {analysis.roi.monthly_roi_pct:.1f}%",
    )

    if a.bounce_scenario:
        st.markdown("**Bounce scenario**")
        st.info(a.bounce_scenario)
    if a.assignment_scenario:
        st.markdown("**Assignment scenario**")
        st.warning(a.assignment_scenario)
    if a.key_risks:
        st.markdown("**Key risks**")
        for risk in a.key_risks:
            st.markdown(f"- {risk}")
    if a.reasoning:
        st.markdown("**Reasoning**")
        st.write(a.reasoning)

    with st.expander("Quantitative detail + context used"):
        st.json(analysis.roi.model_dump())
        st.json(analysis.context)
