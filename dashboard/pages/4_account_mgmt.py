"""Account management page — balance init, manual adjustments, account reset (Day 12)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Account Management", layout="wide")
st.title("Account Management")

account_id = st.selectbox("Account", settings.PAPER_ACCOUNTS)


def _get_account(account_id: str):  # type: ignore[return]
    from paper_trading.paper_account import PaperAccount
    return PaperAccount(account_id=account_id, accounts_dir=settings.PAPER_ACCOUNTS_DIR)


# ── Current state ─────────────────────────────────────────────────────────────
st.subheader("Current State")
try:
    acct = _get_account(account_id)
    state = acct.get_state()

    col1, col2, col3 = st.columns(3)
    col1.metric("Cash", f"${state.cash:,.2f}")
    col2.metric("Equity", f"${state.equity:,.2f}")
    col3.metric("Open Positions", len(state.open_positions))

    if state.initial_cash:
        st.caption(f"Initial balance: ${state.initial_cash:,.2f}  |  Account ID: {account_id}")
except Exception as exc:
    st.info(f"Account not yet initialized: {exc}")

st.divider()

# ── Initialize balance ────────────────────────────────────────────────────────
st.subheader("Initialize Balance")
with st.form("init_balance"):
    init_amount = st.number_input("Starting balance ($)", min_value=1000.0, value=50000.0, step=1000.0)
    init_note = st.text_input("Note", value="Starting paper trading balance")
    submitted = st.form_submit_button("Initialize")
    if submitted:
        try:
            acct = _get_account(account_id)
            acct.initialize_balance(cash=init_amount, note=init_note)
            st.success(f"Balance initialized at ${init_amount:,.2f}")
        except Exception as exc:
            st.error(f"Failed: {exc}")

st.divider()

# ── Manual adjustment ─────────────────────────────────────────────────────────
st.subheader("Manual Adjustment")
with st.form("manual_adj"):
    adj_amount = st.number_input(
        "Amount ($, positive=deposit, negative=withdrawal)",
        min_value=-1_000_000.0, max_value=1_000_000.0, value=0.0, step=100.0,
    )
    adj_reason = st.text_input("Reason", value="Manual adjustment")
    submitted_adj = st.form_submit_button("Apply Adjustment")
    if submitted_adj:
        if adj_amount == 0.0:
            st.warning("Amount is 0 — no change applied.")
        else:
            try:
                acct = _get_account(account_id)
                acct.manual_adjustment(amount=adj_amount, reason=adj_reason)
                st.success(f"Adjustment of ${adj_amount:+,.2f} applied.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

st.divider()

# ── Reset account ─────────────────────────────────────────────────────────────
st.subheader("Reset Account")
st.warning("Resetting will wipe all open positions and start from a new balance. Trade history is preserved.")

with st.form("reset_account"):
    reset_balance = st.number_input("New starting balance ($)", min_value=1000.0, value=50000.0, step=1000.0)
    confirm_text = st.text_input("Type RESET to confirm")
    submitted_reset = st.form_submit_button("Reset Account", type="primary")
    if submitted_reset:
        if confirm_text != "RESET":
            st.error("Type RESET exactly to confirm.")
        else:
            try:
                acct = _get_account(account_id)
                acct.reset(new_balance=reset_balance, confirm=True)
                st.success(f"Account {account_id} reset. New balance: ${reset_balance:,.2f}")
            except Exception as exc:
                st.error(f"Reset failed: {exc}")

st.divider()

# ── Balance history ───────────────────────────────────────────────────────────
st.subheader("Balance History")
try:
    acct = _get_account(account_id)
    state = acct.get_state()
    history = state.balance_history
    if history:
        import pandas as pd
        df = pd.DataFrame([h.model_dump() for h in history])
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No balance history yet.")
except Exception:
    st.info("Could not load balance history.")
