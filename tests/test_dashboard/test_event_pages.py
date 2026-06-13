"""Tests for the Phase 3 dashboard pages (earnings + event plays)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock


from data.db import get_connection, init_db

_PAGES = Path("dashboard/pages")


def _load_page(monkeypatch, filename: str):
    """Import a Streamlit page module with Streamlit mocked out.

    ``st.stop()`` raises SystemExit (as real Streamlit does), which we swallow —
    the module-level functions are still bound and returnable.
    """
    mock_st = MagicMock()
    mock_st.set_page_config = MagicMock()
    mock_st.columns = MagicMock(return_value=[MagicMock(), MagicMock(), MagicMock()])
    mock_st.stop = MagicMock(side_effect=SystemExit(0))
    monkeypatch.setitem(sys.modules, "streamlit", mock_st)

    path = _PAGES / filename
    spec = importlib.util.spec_from_file_location(f"_page_{filename}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass  # st.stop() on an empty DB — expected
    return module


# ── existence ─────────────────────────────────────────────────────────────────
def test_earnings_page_exists() -> None:
    assert (_PAGES / "8_earnings.py").exists()


def test_event_plays_page_exists() -> None:
    assert (_PAGES / "9_event_plays.py").exists()


# ── earnings page load function ─────────────────────────────────────────────────
def test_earnings_load_empty(monkeypatch, tmp_path) -> None:
    module = _load_page(monkeypatch, "8_earnings.py")
    db = str(tmp_path / "t.db")
    init_db(db)
    df = module.load_opportunities(db)
    assert df.empty


def test_earnings_load_returns_rows(monkeypatch, tmp_path) -> None:
    module = _load_page(monkeypatch, "8_earnings.py")
    db = str(tmp_path / "t.db")
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """INSERT INTO earnings_opportunities
               (ticker, earnings_date, earnings_time, strategy, iv_rank,
                expected_move, avg_iv_crush, breach_rate, overall_score, priority)
               VALUES ('NVDA','2026-07-01','AMC','IV_CRUSH',70,8.0,24.0,0.1,82,'HIGH')""",
        )
    df = module.load_opportunities(db)
    assert len(df) == 1
    assert df.iloc[0]["ticker"] == "NVDA"
    assert df.iloc[0]["strategy"] == "IV_CRUSH"


# ── event plays page load function ──────────────────────────────────────────────
def test_event_load_empty(monkeypatch, tmp_path) -> None:
    module = _load_page(monkeypatch, "9_event_plays.py")
    db = str(tmp_path / "t.db")
    init_db(db)
    assert module.load_drops(db).empty


def test_event_load_returns_rows(monkeypatch, tmp_path) -> None:
    module = _load_page(monkeypatch, "9_event_plays.py")
    db = str(tmp_path / "t.db")
    init_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """INSERT INTO drop_signals
               (ticker, drop_pct, classification, confidence, cause_summary,
                bounce_confidence, bounce_score, trade_recommendation)
               VALUES ('TSLA',6.0,'PURE_SENTIMENT',90,'tweet drama',85,82,'STRONG_BUY')""",
        )
    df = module.load_drops(db)
    assert len(df) == 1
    assert df.iloc[0]["classification"] == "PURE_SENTIMENT"
