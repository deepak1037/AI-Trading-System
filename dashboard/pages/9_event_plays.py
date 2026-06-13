"""Event Plays page — recent drops, classification, and bounce setups (Phase 3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from config.settings import settings
from data.db import get_connection

st.set_page_config(page_title="Event Plays", layout="wide")
st.title("📉 Event Plays — Drop / Bounce Detector")

# Classification → row tint.
_CLS_COLOR = {
    "PURE_SENTIMENT": "#1b5e20",   # green — best bounce
    "HYBRID": "#0d47a1",           # blue — recoverable
    "EARNINGS_MISS": "#5d4037",    # brown — depends
    "FUNDAMENTAL": "#b71c1c",      # red — do not trade
}


def load_drops(db_path: str | None = None, limit: int = 100) -> pd.DataFrame:
    """Most-recent classified drops as a DataFrame."""
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT ticker, drop_pct, classification, confidence, cause_summary,
                          bounce_confidence, bounce_score, trade_recommendation,
                          created_at
                   FROM drop_signals
                   ORDER BY created_at DESC, bounce_score DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _style_cls(val: str) -> str:
    return f"background-color: {_CLS_COLOR.get(val, '#424242')}; color: white"


df = load_drops(settings.DB_PATH)

st.subheader("Current Drop Alerts")
if df.empty:
    st.info(
        "No drops classified yet. The drop classifier logs here when a watched "
        "stock falls more than "
        f"{settings.DROP_ALERT_THRESHOLD_PCT:.0f}% over {settings.DROP_LOOKBACK_DAYS} days."
    )
    st.stop()

bounce_candidates = df[df["classification"].isin(["PURE_SENTIMENT", "HYBRID"])]
c1, c2, c3 = st.columns(3)
c1.metric("Drops tracked", len(df))
c2.metric("Bounce candidates", len(bounce_candidates))
c3.metric("Avoid (fundamental)", int((df["classification"] == "FUNDAMENTAL").sum()))

st.dataframe(
    df.style.map(_style_cls, subset=["classification"]),
    use_container_width=True,
)

st.divider()
st.subheader("Historical Event-Play Performance")
st.caption(
    "Classification accuracy and win rate by classification type populate here "
    "as event-play positions close."
)
by_cls = (
    df.groupby("classification")
    .agg(count=("ticker", "count"),
         avg_bounce=("bounce_confidence", "mean"),
         avg_score=("bounce_score", "mean"))
    .reset_index()
)
st.dataframe(by_cls, use_container_width=True)

st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s")
