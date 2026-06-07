"""Order entry page — place or preview equity and options orders (manual trading)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

from config.settings import settings

st.set_page_config(page_title="Place Order", layout="wide")
st.title("Place Order")

# ── Environment banner ────────────────────────────────────────────────────────

if settings.ENV == "live" and not settings.DRY_RUN and settings.LIVE_TRADING_ENABLED:
    st.error("LIVE TRADING ENABLED — orders placed here use real money.")
elif settings.ENV == "paper":
    st.info("Paper mode — orders validated via Schwab previewOrder API. No real trade.")
else:
    st.warning(f"ENV={settings.ENV} | DRY_RUN={settings.DRY_RUN} — orders will be logged only.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_broker():
    from broker_core.factory import get_broker
    return get_broker()

def _get_router(broker):
    from broker_client.order_router import OrderRouter
    return OrderRouter(broker=broker)


# ── Order type tabs ───────────────────────────────────────────────────────────

tab_equity, tab_options = st.tabs(["Equity", "Options"])

# ════════════════════════════════════════════════════════════════════════
# EQUITY TAB
# ════════════════════════════════════════════════════════════════════════
with tab_equity:
    st.subheader("Equity Order")

    col1, col2, col3 = st.columns(3)
    with col1:
        eq_ticker = st.text_input("Ticker", value="", key="eq_ticker",
                                  placeholder="AAPL").upper().strip()
        eq_action = st.selectbox("Action", ["BUY", "SELL"], key="eq_action")
    with col2:
        eq_qty = st.number_input("Quantity (shares)", min_value=1, value=10,
                                  step=1, key="eq_qty")
        eq_order_type = st.selectbox("Order Type",
                                     ["limit", "market", "stop", "stop_limit"],
                                     key="eq_order_type")
    with col3:
        eq_limit = None
        eq_stop = None
        if eq_order_type in ("limit", "stop_limit"):
            eq_limit = st.number_input("Limit Price", min_value=0.01,
                                        value=100.00, step=0.01,
                                        format="%.2f", key="eq_limit")
        if eq_order_type in ("stop", "stop_limit"):
            eq_stop = st.number_input("Stop Price", min_value=0.01,
                                       value=99.00, step=0.01,
                                       format="%.2f", key="eq_stop")
        eq_strategy = st.text_input("Strategy tag", value="manual",
                                     key="eq_strategy")

    st.divider()
    col_prev, col_place = st.columns(2)

    with col_prev:
        if st.button("Preview Order", key="eq_preview", use_container_width=True):
            if not eq_ticker:
                st.error("Enter a ticker.")
            else:
                with st.spinner("Calling Schwab previewOrder..."):
                    try:
                        from broker_core.base_broker import Order
                        order = Order(
                            ticker=eq_ticker,
                            action=eq_action,
                            qty=int(eq_qty),
                            order_type=eq_order_type,
                            limit_price=eq_limit,
                            stop_price=eq_stop,
                            strategy_name=eq_strategy,
                            account_id=settings.SCHWAB_ACCOUNT_NUMBER or "paper_main",
                        )
                        broker = _get_broker()
                        preview = broker.preview_order(order)
                        if preview.is_valid:
                            st.success("Order is valid")
                        else:
                            st.error(f"Rejected: {preview.rejection_reason}")
                        st.json({
                            "estimated_cost":      f"${preview.estimated_cost:,.2f}",
                            "buying_power_effect": f"${preview.buying_power_effect:,.2f}",
                            "fees":                f"${preview.fees:,.2f}",
                            "is_valid":            preview.is_valid,
                            "rejection_reason":    preview.rejection_reason,
                        })
                    except Exception as exc:
                        st.error(f"Preview failed: {exc}")

    with col_place:
        place_label = "Place LIVE Order" if (
            settings.ENV == "live" and not settings.DRY_RUN
            and settings.LIVE_TRADING_ENABLED
        ) else "Submit Order (paper/dev)"

        if st.button(place_label, key="eq_place", use_container_width=True,
                     type="primary"):
            if not eq_ticker:
                st.error("Enter a ticker.")
            else:
                confirm = st.session_state.get("eq_confirmed", False)
                if settings.ENV == "live" and not settings.DRY_RUN and not confirm:
                    st.session_state["eq_confirmed"] = True
                    st.warning("Click **Submit Order** again to confirm the LIVE order.")
                else:
                    st.session_state["eq_confirmed"] = False
                    with st.spinner("Submitting..."):
                        try:
                            from broker_core.base_broker import Order
                            order = Order(
                                ticker=eq_ticker,
                                action=eq_action,
                                qty=int(eq_qty),
                                order_type=eq_order_type,
                                limit_price=eq_limit,
                                stop_price=eq_stop,
                                strategy_name=eq_strategy,
                                account_id=settings.SCHWAB_ACCOUNT_NUMBER or "paper_main",
                            )
                            broker = _get_broker()
                            router = _get_router(broker)
                            result = router.execute(order)
                            st.success(f"Order submitted — ID: {result.order_id} | Status: {result.status}")
                            st.json({
                                "order_id":   result.order_id,
                                "status":     result.status,
                                "fill_price": result.fill_price,
                                "commission": result.commission,
                            })
                        except Exception as exc:
                            st.error(f"Order failed: {exc}")

# ════════════════════════════════════════════════════════════════════════
# OPTIONS TAB
# ════════════════════════════════════════════════════════════════════════
with tab_options:
    st.subheader("Options Order")
    st.caption(
        "Contract symbol format: `AAPL  260620P00190000`  "
        "(underlying padded to 6 chars + YYMMDD + C/P + strike×1000 zero-padded to 8 digits)"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        opt_ticker = st.text_input("Underlying", value="",
                                    placeholder="AAPL", key="opt_ticker").upper().strip()
        opt_action = st.selectbox(
            "Action",
            ["SELL_TO_OPEN", "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_CLOSE"],
            key="opt_action",
        )
    with col2:
        opt_contract = st.text_input(
            "OCC Contract Symbol", value="", key="opt_contract",
            placeholder="AAPL  260620P00190000",
        ).strip()
        opt_qty = st.number_input("Contracts", min_value=1, value=1,
                                   step=1, key="opt_qty")
    with col3:
        opt_limit = st.number_input("Limit Price (per share)", min_value=0.01,
                                     value=1.00, step=0.01,
                                     format="%.2f", key="opt_limit",
                                     help="Options orders always use limit. "
                                          "Market orders for options are blocked.")
        opt_strategy = st.text_input("Strategy tag", value="manual",
                                      key="opt_strategy")

    st.divider()

    # OCC symbol builder helper
    with st.expander("OCC Symbol Builder"):
        b_col1, b_col2, b_col3, b_col4 = st.columns(4)
        with b_col1:
            b_under = st.text_input("Underlying", key="b_under", placeholder="AAPL").upper()
        with b_col2:
            b_expiry = st.date_input("Expiry", key="b_expiry")
        with b_col3:
            b_type = st.selectbox("Type", ["P", "C"], key="b_type")
        with b_col4:
            b_strike = st.number_input("Strike", min_value=0.5, value=190.0,
                                        step=0.5, format="%.1f", key="b_strike")
        if b_under and b_expiry and b_strike:
            occ = f"{b_under:<6}{b_expiry.strftime('%y%m%d')}{b_type}{int(b_strike * 1000):08d}"
            st.code(occ)
            if st.button("Use this symbol", key="b_use"):
                st.session_state["opt_contract"] = occ
                st.session_state["opt_ticker"] = b_under
                st.rerun()

    col_prev2, col_place2 = st.columns(2)

    with col_prev2:
        if st.button("Preview Options Order", key="opt_preview",
                     use_container_width=True):
            if not opt_contract:
                st.error("Enter a contract symbol.")
            else:
                with st.spinner("Calling Schwab previewOrder..."):
                    try:
                        from broker_core.base_broker import OptionsOrder
                        order = OptionsOrder(
                            ticker=opt_ticker or opt_contract[:6].strip(),
                            action=opt_action,
                            contract=opt_contract,
                            qty=int(opt_qty),
                            order_type="limit",
                            limit_price=float(opt_limit),
                            strategy_name=opt_strategy,
                        )
                        broker = _get_broker()
                        preview = broker.preview_options_order(order)
                        if preview.is_valid:
                            st.success("Order is valid")
                        else:
                            st.error(f"Rejected: {preview.rejection_reason}")
                        st.json({
                            "estimated_cost":      f"${preview.estimated_cost:,.2f}",
                            "buying_power_effect": f"${preview.buying_power_effect:,.2f}",
                            "fees":                f"${preview.fees:,.2f}",
                            "is_valid":            preview.is_valid,
                            "rejection_reason":    preview.rejection_reason,
                        })
                    except Exception as exc:
                        st.error(f"Preview failed: {exc}")

    with col_place2:
        opt_place_label = "Place LIVE Options Order" if (
            settings.ENV == "live" and not settings.DRY_RUN
            and settings.LIVE_TRADING_ENABLED
        ) else "Submit Options Order (paper/dev)"

        if st.button(opt_place_label, key="opt_place", use_container_width=True,
                     type="primary"):
            if not opt_contract:
                st.error("Enter a contract symbol.")
            else:
                confirm = st.session_state.get("opt_confirmed", False)
                if settings.ENV == "live" and not settings.DRY_RUN and not confirm:
                    st.session_state["opt_confirmed"] = True
                    st.warning("Click **Submit Options Order** again to confirm the LIVE order.")
                else:
                    st.session_state["opt_confirmed"] = False
                    with st.spinner("Submitting..."):
                        try:
                            from broker_core.base_broker import OptionsOrder
                            order = OptionsOrder(
                                ticker=opt_ticker or opt_contract[:6].strip(),
                                action=opt_action,
                                contract=opt_contract,
                                qty=int(opt_qty),
                                order_type="limit",
                                limit_price=float(opt_limit),
                                strategy_name=opt_strategy,
                            )
                            broker = _get_broker()
                            router = _get_router(broker)
                            result = router.execute(order)
                            st.success(f"Order submitted — ID: {result.order_id} | Status: {result.status}")
                            st.json({
                                "order_id":   result.order_id,
                                "status":     result.status,
                                "fill_price": result.fill_price,
                                "commission": result.commission,
                            })
                        except Exception as exc:
                            st.error(f"Order failed: {exc}")
