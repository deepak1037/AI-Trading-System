"""Performance page — metrics, trade log (filterable), signal accuracy (Day 12)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Performance", layout="wide")
st.title("Performance")


def _load_account_state(account_id: str) -> "dict | None":
    import json

    path = Path(settings.PAPER_ACCOUNTS_DIR) / f"{account_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)  # type: ignore[no-any-return]


def _load_trades_db(account_id: str) -> pd.DataFrame:
    db_path = Path(settings.DB_PATH)
    if not db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(str(db_path)) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM trades WHERE account_id = ? ORDER BY timestamp DESC LIMIT 500",
            conn,
            params=(account_id,),
        )
    return df


def _load_accuracy_db(days: int = 30) -> pd.DataFrame:
    db_path = Path(settings.DB_PATH)
    if not db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(str(db_path)) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM accuracy_log WHERE date >= date('now', ?) ORDER BY date DESC",
            conn,
            params=(f"-{days} days",),
        )
    return df


# ── Account selector ──────────────────────────────────────────────────────────
account_id = st.selectbox("Account", settings.PAPER_ACCOUNTS)
state = _load_account_state(account_id)

# ── Performance metrics ───────────────────────────────────────────────────────
st.subheader("Performance Metrics")

if state:
    closed = state.get("closed_positions", [])
    if closed:
        pnl_series = [float(p.get("realized_pnl", 0)) for p in closed if p.get("realized_pnl") is not None]
        if pnl_series:
            try:
                from paper_trading.performance import compute_metrics
                metrics = compute_metrics(pnl_series)
                cols = st.columns(5)
                cols[0].metric("Sharpe Ratio", f"{metrics.sharpe_ratio:.2f}")
                cols[1].metric("Sortino Ratio", f"{metrics.sortino_ratio:.2f}")
                cols[2].metric("Max Drawdown", f"{metrics.max_drawdown_pct:.1f}%")
                cols[3].metric("Win Rate", f"{metrics.win_rate_pct:.1f}%")
                cols[4].metric("Profit Factor", f"{metrics.profit_factor:.2f}")
            except Exception as exc:
                st.warning(f"Could not compute metrics: {exc}")
    else:
        st.info("No closed positions yet.")
else:
    st.info(f"Account {account_id} not initialized.")

st.divider()

# ── Trade log ─────────────────────────────────────────────────────────────────
st.subheader("Trade Log")

df_trades = _load_trades_db(account_id)

if not df_trades.empty:
    col1, col2 = st.columns(2)
    with col1:
        strategy_filter = st.multiselect(
            "Filter by strategy",
            options=df_trades["strategy"].unique().tolist() if "strategy" in df_trades.columns else [],
        )
    with col2:
        action_filter = st.multiselect(
            "Filter by action",
            options=df_trades["action"].unique().tolist() if "action" in df_trades.columns else [],
        )

    filtered = df_trades.copy()
    if strategy_filter:
        filtered = filtered[filtered["strategy"].isin(strategy_filter)]
    if action_filter:
        filtered = filtered[filtered["action"].isin(action_filter)]

    st.dataframe(filtered, use_container_width=True)
    st.caption(f"{len(filtered)} trades shown")
else:
    st.info("No trade history in database yet.")

st.divider()

# ── Signal accuracy ───────────────────────────────────────────────────────────
st.subheader("Signal Accuracy (30 days)")

days_back = st.slider("Days back", 7, 90, 30)
df_acc = _load_accuracy_db(days=days_back)

if not df_acc.empty and "was_correct" in df_acc.columns:
    filled = df_acc[df_acc["was_correct"].notna()]
    if not filled.empty:
        accuracy = filled["was_correct"].mean() * 100
        st.metric("Direction Accuracy", f"{accuracy:.1f}%",
                  delta="above target" if accuracy >= 65 else "below 65% target")
        st.dataframe(filled[["date", "predicted_direction", "actual_direction",
                              "actual_pct_move", "was_correct"]].head(50),
                     use_container_width=True)
    else:
        st.info("Accuracy log exists but no EOD actuals filled in yet.")
else:
    st.info("No signal accuracy data yet.")

st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s")
