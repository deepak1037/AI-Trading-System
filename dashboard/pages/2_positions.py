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


# ── Source selector ───────────────────────────────────────────────────────────

live_label = f"{settings.BROKER}_live"
sources = [live_label] + list(settings.PAPER_ACCOUNTS)

source = st.selectbox("Account", sources)


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_paper_positions(account_id: str) -> list[dict]:
    import json
    path = Path(settings.PAPER_ACCOUNTS_DIR) / f"{account_id}.json"
    if not path.exists():
        st.warning(f"Account file not found: {path}")
        return []
    with open(path) as f:
        state = json.load(f)
    return state.get("open_positions", [])  # type: ignore[no-any-return]


def _strategy_map_from_db() -> dict[str, str]:
    """Return {ticker: strategy} for all open positions in SQLite."""
    import sqlite3
    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT ticker, strategy FROM positions WHERE is_open=1"
            ).fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}


def _load_broker_positions() -> list[dict]:
    try:
        from broker_core.factory import get_broker
        broker = get_broker()
        raw = broker.get_positions()
        db_strategies = _strategy_map_from_db()
        return [
            {
                "ticker": p.ticker,
                "strategy": db_strategies.get(p.ticker, "manual"),
                "position_type": p.position_type,
                "qty": p.qty,
                "entry_price": p.avg_cost,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "stop_loss": None,
                "take_profit": None,
                "entry_date": str(p.opened_at.date()) if p.opened_at else "",
            }
            for p in raw
        ]
    except Exception as exc:
        st.error(f"Failed to load {settings.BROKER} positions: {exc}")
        return []


def _get_live_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ── Load positions ────────────────────────────────────────────────────────────

if source == live_label:
    with st.spinner(f"Fetching positions from {settings.BROKER}..."):
        positions = _load_broker_positions()
else:
    positions = _load_paper_positions(source)

if not positions:
    st.info("No open positions.")
    st.stop()


# ── Build display table ───────────────────────────────────────────────────────

rows = []
for pos in positions:
    ticker = pos.get("ticker", "")
    qty = pos.get("qty", 0)
    entry = pos.get("entry_price", 0)
    pos_type = pos.get("position_type", "")
    multiplier = 100 if "option" in pos_type else 1

    if source == live_label:
        # Broker supplies current_price (per-share) and unrealized_pnl directly.
        # Do NOT fall back to a computed formula — broker values may be 0 for
        # positions opened today, but a computed fallback using stale prices
        # produces nonsense (especially for options where multiplier=100).
        live_price = pos.get("current_price") or entry
        unrealized_pnl = pos.get("unrealized_pnl") or 0.0
    else:
        live_price = _get_live_price(ticker) or pos.get("current_price", entry)
        unrealized_pnl = (live_price - entry) * qty * multiplier

    cost_basis = abs(entry * qty * multiplier)
    pnl_pct = unrealized_pnl / cost_basis * 100 if cost_basis else 0.0

    rows.append({
        "Ticker": ticker,
        "Strategy": pos.get("strategy", "—"),
        "Type": pos.get("position_type", "—"),
        "Qty": qty,
        "Entry": f"${entry:.2f}",
        "Live Price": f"${live_price:.2f}",
        "Unrealized P&L": f"${unrealized_pnl:+,.2f}",
        "P&L %": f"{pnl_pct:+.2f}%",
        "Stop Loss": f"${pos['stop_loss']:.2f}" if pos.get("stop_loss") else "—",
        "Take Profit": f"${pos['take_profit']:.2f}" if pos.get("take_profit") else "—",
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

total_pnl = sum(
    float(r["Unrealized P&L"].replace("$", "").replace(",", ""))
    for r in rows
)
col1, col2 = st.columns(2)
col1.metric("Total Unrealized P&L", f"${total_pnl:+,.2f}")
col2.metric("Open Positions", len(positions))

st.caption(
    f"Source: {settings.BROKER.title() + ' Live API' if source == live_label else source} | "
    f"Auto-refresh: every {settings.DASHBOARD_REFRESH_SECONDS}s"
)
