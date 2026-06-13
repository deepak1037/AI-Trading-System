"""Earnings Analyzer page — upcoming IV crush / spike opportunities (Phase 3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from config.settings import settings
from data.db import get_connection

st.set_page_config(page_title="Earnings Analyzer", layout="wide")
st.title("🎯 Earnings Analyzer")

# Strategy → row tint (green=crush, blue=spike, gray=skip).
_STRATEGY_COLOR = {"IV_CRUSH": "#1b5e20", "IV_SPIKE": "#0d47a1", "SKIP": "#424242"}


def load_opportunities(db_path: str | None = None, limit: int = 100) -> pd.DataFrame:
    """Most-recent persisted earnings opportunities as a DataFrame."""
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT ticker, earnings_date, earnings_time, strategy, iv_rank,
                          expected_move, avg_iv_crush, breach_rate, overall_score,
                          priority, created_at
                   FROM earnings_opportunities
                   ORDER BY created_at DESC, overall_score DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception:  # noqa: BLE001 — empty frame if the table isn't there yet
        return pd.DataFrame()


def _style_strategy(val: str) -> str:
    return f"background-color: {_STRATEGY_COLOR.get(val, '#424242')}; color: white"


df = load_opportunities(settings.DB_PATH)

st.subheader("Upcoming Earnings Opportunities")
if df.empty:
    st.info(
        "No earnings opportunities stored yet. Run `make earnings` (or "
        "`python -m broker_client.earnings.cli --save`) to populate this page."
    )
    st.stop()

actionable = df[df["strategy"] != "SKIP"]
c1, c2, c3 = st.columns(3)
c1.metric("Opportunities", len(df))
c2.metric("Actionable", len(actionable))
c3.metric("IV Crush / IV Spike",
          f"{(df['strategy'] == 'IV_CRUSH').sum()} / {(df['strategy'] == 'IV_SPIKE').sum()}")

styled = actionable if not actionable.empty else df
st.dataframe(
    styled.style.map(_style_strategy, subset=["strategy"]),
    use_container_width=True,
)

st.divider()
st.subheader("Historical Earnings Performance")
st.caption(
    "Win rate by strategy, avg IV crush captured, and breach-rate vs prediction "
    "appear here once trades close (tracked in the trades table)."
)
if not df.empty:
    by_strat = (
        df.groupby("strategy")
        .agg(plays=("ticker", "count"),
             avg_score=("overall_score", "mean"),
             avg_crush=("avg_iv_crush", "mean"))
        .reset_index()
    )
    st.dataframe(by_strat, use_container_width=True)

st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s")
