"""Streamlit dashboard entry point (Day 12).

Multi-page app. Run with:
    streamlit run dashboard/app.py --server.port 8501
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

st.set_page_config(
    page_title="AI Trading System",
    page_icon=":chart_with_upwards_trend:",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("AI Trading System")
st.sidebar.markdown("---")
st.sidebar.markdown("**Navigation**")

pages = {
    "Overview": "pages/1_overview",
    "Positions": "pages/2_positions",
    "Performance": "pages/3_performance",
    "Account Mgmt": "pages/4_account_mgmt",
}

for name in pages:
    st.sidebar.markdown(f"- {name}")

st.title("AI Trading System")
st.markdown(
    "Welcome to the AI Trading System dashboard. Use the sidebar to navigate between pages "
    "or select a page from the top navigation."
)

st.info(
    "Dashboard running. Navigate using the sidebar pages or the pages/ directory in Streamlit."
)
