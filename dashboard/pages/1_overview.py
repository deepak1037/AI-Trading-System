"""Overview page — paper vs live account cards + equity curve vs SPY (Day 12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Overview", layout="wide")
st.title("Overview")


def _load_account_state(account_id: str) -> dict | None:
    import json
    from pathlib import Path

    path = Path(settings.PAPER_ACCOUNTS_DIR) / f"{account_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)  # type: ignore[no-any-return]


# ── Account summary cards ─────────────────────────────────────────────────────
st.subheader("Account Summary")
cols = st.columns(len(settings.PAPER_ACCOUNTS) + 1)

for i, account_id in enumerate(settings.PAPER_ACCOUNTS):
    state = _load_account_state(account_id)
    with cols[i]:
        if state:
            equity = state.get("equity", 0)
            cash = state.get("cash", 0)
            initial = state.get("initial_cash", 1)
            ret_pct = (equity - initial) / initial * 100 if initial > 0 else 0.0
            open_pos = len(state.get("open_positions", []))

            st.metric(
                label=account_id,
                value=f"${equity:,.2f}",
                delta=f"{ret_pct:+.2f}%",
            )
            st.caption(f"Cash: ${cash:,.2f} | Positions: {open_pos}")
        else:
            st.metric(label=account_id, value="Not initialized")
            st.caption("Run initialize_balance() to set up account")


# ── Equity curve vs benchmark ─────────────────────────────────────────────────
st.subheader("Equity Curve vs SPY")

account_id = settings.PAPER_ACCOUNTS[0] if settings.PAPER_ACCOUNTS else "paper_main"
state = _load_account_state(account_id)

if state and state.get("daily_equity_history"):
    hist = state["daily_equity_history"]
    df_eq = pd.DataFrame(hist).set_index("date")
    df_eq.index = pd.to_datetime(df_eq.index)

    # Fetch SPY for same period
    try:
        import yfinance as yf

        start = str(df_eq.index.min().date())
        end = str(df_eq.index.max().date())
        spy_data = yf.Ticker(settings.DASHBOARD_BENCHMARK_TICKER).history(
            start=start, end=end, interval="1d"
        )
        spy_norm = spy_data["Close"] / spy_data["Close"].iloc[0] * state.get("initial_cash", 50000)

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_eq.index, y=df_eq["equity"],
            mode="lines", name=account_id, line={"color": "blue"},
        ))
        fig.add_trace(go.Scatter(
            x=spy_norm.index, y=spy_norm.values,
            mode="lines", name=settings.DASHBOARD_BENCHMARK_TICKER,
            line={"color": "orange", "dash": "dash"},
        ))
        fig.update_layout(
            title="Portfolio Equity vs SPY",
            xaxis_title="Date",
            yaxis_title="Value ($)",
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.warning(f"Could not fetch SPY benchmark: {exc}")
        st.line_chart(df_eq["equity"])
else:
    st.info("No equity history yet. Start paper trading to see the equity curve.")


# ── Three-bucket summary (Phase 2) ────────────────────────────────────────────
st.divider()
st.subheader("Bucket Summary")
try:
    from broker_client.buckets.bucket_manager import BucketManager

    _bm = BucketManager()
    _summary = _bm.get_bucket_summary()
    _names = {
        1: "Bucket 1 — Income Engine (MSP/Wheel)",
        2: "Bucket 2 — Earnings Plays",
        3: "Bucket 3 — Event/LEAP",
    }
    _allocs = {
        1: settings.BUCKET1_ALLOCATION_PCT,
        2: settings.BUCKET2_ALLOCATION_PCT,
        3: settings.BUCKET3_ALLOCATION_PCT,
    }
    bcols = st.columns(3)
    for _i, _b in enumerate((1, 2, 3)):
        _pnl = _summary[_b]
        with bcols[_i]:
            st.markdown(f"**{_names[_b]}**")
            st.metric(
                f"Positions ({_allocs[_b]}% alloc)",
                _pnl.open_positions,
                f"{_pnl.mtd_return:+.2f}% MTD",
            )
            st.caption(
                f"Premium: ${_pnl.premium_collected:,.0f} | "
                f"Realized: ${_pnl.realized_pnl:+,.0f} | "
                f"Win rate: {_pnl.win_rate:.0f}%"
            )
    st.caption(f"Cash buffer target: {settings.cash_buffer_pct}%")
except Exception as exc:
    st.info(f"Bucket summary unavailable: {exc}")


# ── Auto-refresh ──────────────────────────────────────────────────────────────
st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s")
