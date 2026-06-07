"""Positions page — open positions with live P&L, stop/target levels (Day 12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Positions", layout="wide")
st.title("Open Positions")


def _load_account_state(account_id: str) -> "dict | None":
    import json

    path = Path(settings.PAPER_ACCOUNTS_DIR) / f"{account_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)  # type: ignore[no-any-return]


def _get_live_price(ticker: str) -> float | None:
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        hist = t.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


account_id = st.selectbox("Account", settings.PAPER_ACCOUNTS)
state = _load_account_state(account_id)

if not state:
    st.warning(f"Account {account_id} not initialized.")
    st.stop()

positions = state.get("open_positions", [])
if not positions:
    st.info("No open positions.")
    st.stop()

rows = []
for pos in positions:
    ticker = pos.get("ticker", "")
    live_price = _get_live_price(ticker) or pos.get("current_price", pos.get("entry_price", 0))
    qty = pos.get("qty", 0)
    entry = pos.get("entry_price", 0)
    unrealized_pnl = (live_price - entry) * qty
    pnl_pct = (live_price - entry) / entry * 100 if entry else 0

    rows.append({
        "Ticker": ticker,
        "Strategy": pos.get("strategy", ""),
        "Type": pos.get("position_type", ""),
        "Qty": qty,
        "Entry": f"${entry:.2f}",
        "Live Price": f"${live_price:.2f}",
        "Unrealized P&L": f"${unrealized_pnl:+,.2f}",
        "P&L %": f"{pnl_pct:+.2f}%",
        "Stop Loss": f"${pos.get('stop_loss', 0):.2f}" if pos.get("stop_loss") else "—",
        "Take Profit": f"${pos.get('take_profit', 0):.2f}" if pos.get("take_profit") else "—",
        "Entry Date": pos.get("entry_date", ""),
    })

df = pd.DataFrame(rows)

def _color_pnl(val: str) -> str:
    if "+" in str(val):
        return "color: green"
    if "-" in str(val):
        return "color: red"
    return ""

styled = df.style.applymap(_color_pnl, subset=["Unrealized P&L", "P&L %"])
st.dataframe(styled, use_container_width=True)

# Summary metrics
total_pnl = sum(
    ((_get_live_price(p["ticker"]) or p.get("current_price", p["entry_price"])) - p["entry_price"])
    * p["qty"]
    for p in positions
)
st.metric("Total Unrealized P&L", f"${total_pnl:+,.2f}")
st.caption(f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s | {len(positions)} open positions")
