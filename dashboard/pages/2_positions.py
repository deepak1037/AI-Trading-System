"""Positions page — open positions with live P&L, stop/target levels (Day 12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from datetime import UTC

import pandas as pd
import streamlit as st

from config.settings import settings
from dashboard.bucket_badges import (
    bucket_badge,
    dte_progress_bar,
    recoverable_badge,
    thesis_label,
)

st.set_page_config(page_title="Positions", layout="wide")
st.title("Open Positions")


# ── Source selector ───────────────────────────────────────────────────────────

live_label = f"{settings.BROKER}_live"
sources = [live_label, *settings.PAPER_ACCOUNTS]

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


def _db_meta_from_db() -> dict[str, dict]:
    """Return {ticker: {strategy, stop_loss, take_profit, bucket, ...}} for open positions."""
    import sqlite3
    try:
        with sqlite3.connect(settings.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT ticker, strategy, stop_loss, take_profit, bucket, sub_type, "
                "recoverable, thesis_status, thesis_completion_pct, exit_recommendation "
                "FROM positions WHERE is_open=1"
            ).fetchall()
        return {row["ticker"]: dict(row) for row in rows}
    except Exception:
        return {}


def _strategy_map_from_db() -> dict[str, str]:
    """Return {ticker: strategy} for all open positions in SQLite."""
    return {t: m["strategy"] for t, m in _db_meta_from_db().items()}


def _load_broker_positions() -> list[dict]:
    try:
        from broker_core.factory import get_broker
        broker = get_broker()
        raw = broker.get_positions()
        db_meta = _db_meta_from_db()
        return [
            {
                "ticker": p.ticker,
                "strategy": db_meta.get(p.ticker, {}).get("strategy", "manual"),
                "position_type": p.position_type,
                "qty": p.qty,
                "entry_price": p.avg_cost,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "stop_loss": db_meta.get(p.ticker, {}).get("stop_loss"),
                "take_profit": db_meta.get(p.ticker, {}).get("take_profit"),
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

_db_meta = _db_meta_from_db()
rows = []
for pos in positions:
    ticker = pos.get("ticker", "")
    qty = pos.get("qty", 0)
    entry = pos.get("entry_price", 0)
    pos_type = pos.get("position_type", "")
    multiplier = 100 if "option" in pos_type else 1
    meta = _db_meta.get(ticker, {})

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

    bucket = meta.get("bucket")
    rows.append({
        "Ticker": ticker,
        "Bucket": bucket_badge(bucket, meta.get("sub_type")) if bucket else "—",
        "Risk": recoverable_badge(meta.get("recoverable")) if bucket else "—",
        "Strategy": pos.get("strategy", "—"),
        "Type": pos.get("position_type", "—"),
        "Qty": qty,
        "Entry": f"${entry:.2f}",
        "Live Price": f"${live_price:.2f}",
        "Unrealized P&L": f"${unrealized_pnl:+,.2f}",
        "P&L %": f"{pnl_pct:+.2f}%",
        "DTE": dte_progress_bar(pos.get("dte_remaining"), pos.get("original_dte"))
        if pos.get("dte_remaining") is not None else "—",
        "Thesis": thesis_label(meta.get("thesis_status"), meta.get("thesis_completion_pct"))
        if bucket else "—",
        "Exit Rec": meta.get("exit_recommendation") or "—",
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

# ── Stop-loss / take-profit editor ───────────────────────────────────────────

st.divider()
st.subheader("Set Stop-Loss / Take-Profit")
st.caption(
    "Levels are saved to the SQLite positions table and picked up by PositionWatcher. "
    "For broker positions not yet in SQLite, an entry is created on save."
)

tickers = [pos.get("ticker", "") for pos in positions if pos.get("ticker")]
if tickers:
    edit_ticker = st.selectbox("Select position", tickers, key="sl_tp_ticker")
    current_pos = next((p for p in positions if p.get("ticker") == edit_ticker), {})
    current_sl = current_pos.get("stop_loss") or 0.0
    current_tp = current_pos.get("take_profit") or 0.0
    entry_price = current_pos.get("entry_price", 0.0)

    col_sl, col_tp = st.columns(2)
    with col_sl:
        new_sl = st.number_input(
            f"Stop Loss (entry: ${entry_price:.2f})",
            min_value=0.0,
            value=float(current_sl),
            step=0.5,
            format="%.2f",
            key="sl_input",
            help="PositionWatcher closes the position when price drops to or below this level.",
        )
    with col_tp:
        new_tp = st.number_input(
            f"Take Profit (entry: ${entry_price:.2f})",
            min_value=0.0,
            value=float(current_tp),
            step=0.5,
            format="%.2f",
            key="tp_input",
            help="PositionWatcher closes the position when price rises to or above this level.",
        )

    if st.button("Save Levels", type="primary", key="save_levels"):
        try:
            import sqlite3
            sl_val = float(new_sl) if new_sl > 0 else None
            tp_val = float(new_tp) if new_tp > 0 else None
            with sqlite3.connect(settings.DB_PATH) as conn:
                existing = conn.execute(
                    "SELECT id FROM positions WHERE ticker=? AND is_open=1",
                    (edit_ticker,),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE positions SET stop_loss=?, take_profit=? WHERE ticker=? AND is_open=1",
                        (sl_val, tp_val, edit_ticker),
                    )
                else:
                    # Broker position not yet tracked in SQLite — create stub row
                    from datetime import datetime
                    conn.execute(
                        """INSERT INTO positions
                           (account_id, ticker, strategy, position_type, qty,
                            entry_price, stop_loss, take_profit, opened_at, is_open)
                           VALUES (?,?,?,?,?,?,?,?,?,1)""",
                        (
                            source,
                            edit_ticker,
                            current_pos.get("strategy", "manual"),
                            current_pos.get("position_type", "equity_long"),
                            current_pos.get("qty", 0),
                            current_pos.get("entry_price", 0.0),
                            sl_val,
                            tp_val,
                            datetime.now(tz=UTC).isoformat(),
                        ),
                    )
            sl_str = f"${sl_val:.2f}" if sl_val else "cleared"
            tp_str = f"${tp_val:.2f}" if tp_val else "cleared"
            st.success(f"{edit_ticker} — stop: {sl_str} | target: {tp_str}")
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to save levels: {exc}")
