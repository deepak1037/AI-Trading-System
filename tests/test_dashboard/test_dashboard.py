"""Tests for dashboard modules and PaperAccount reset (Day 12)."""

from __future__ import annotations

from pathlib import Path


def _make_account(tmp_dir: str, balance: float = 50000.0):
    from paper_trading.paper_account import PaperAccount

    acct = PaperAccount(account_id="test_dash", accounts_dir=tmp_dir)
    acct.initialize_balance(cash=balance, note="test init")
    return acct


class TestPaperAccountReset:
    def test_reset_clears_positions_keeps_history(self, tmp_path):
        acct = _make_account(str(tmp_path))
        # Add some balance history
        acct.manual_adjustment(1000.0, reason="bonus")
        state_before = acct.get_state()
        assert len(state_before.balance_history) == 2

        acct.reset(confirm=True, new_balance=25000.0)
        state = acct.get_state()
        assert state.cash == 25000.0
        assert state.equity == 25000.0
        assert state.initial_cash == 25000.0
        assert len(state.open_positions) == 0
        # closed_positions preserved across resets
        assert isinstance(state.closed_positions, list)

    def test_reset_no_confirm_does_nothing(self, tmp_path):
        acct = _make_account(str(tmp_path))
        acct.reset(confirm=False, new_balance=1000.0)
        state = acct.get_state()
        assert state.cash == 50000.0

    def test_reset_with_zero_balance(self, tmp_path):
        acct = _make_account(str(tmp_path))
        acct.reset(confirm=True, new_balance=0.0)
        state = acct.get_state()
        assert state.cash == 0.0


class TestPaperAccountState:
    def test_get_state_returns_model(self, tmp_path):
        from paper_trading.paper_account import AccountState

        acct = _make_account(str(tmp_path))
        state = acct.get_state()
        assert isinstance(state, AccountState)
        assert state.account_id == "test_dash"
        assert state.cash == 50000.0

    def test_balance_history_entries(self, tmp_path):
        acct = _make_account(str(tmp_path))
        acct.manual_adjustment(500.0, reason="deposit")
        state = acct.get_state()
        assert len(state.balance_history) == 2
        actions = [e.action for e in state.balance_history]
        assert "init" in actions
        assert "adjustment" in actions

    def test_get_performance_returns_model(self, tmp_path):
        from paper_trading.paper_account import AccountPerformance

        acct = _make_account(str(tmp_path), balance=100000.0)
        perf = acct.get_performance()
        assert isinstance(perf, AccountPerformance)
        assert perf.initial_cash == 100000.0
        assert perf.total_return_pct == 0.0


class TestDashboardPageImports:
    """Smoke-test that dashboard pages can be imported without Streamlit running."""

    def _patch_streamlit(self, monkeypatch):
        """Prevent any Streamlit calls from executing during import."""
        import sys
        from unittest.mock import MagicMock

        mock_st = MagicMock()
        mock_st.set_page_config = MagicMock()
        mock_st.title = MagicMock()
        mock_st.subheader = MagicMock()
        mock_st.columns = MagicMock(return_value=[MagicMock(), MagicMock(), MagicMock()])
        mock_st.metric = MagicMock()
        mock_st.info = MagicMock()
        mock_st.caption = MagicMock()
        mock_st.stop = MagicMock(side_effect=SystemExit(0))
        monkeypatch.setitem(sys.modules, "streamlit", mock_st)
        monkeypatch.setitem(sys.modules, "plotly", MagicMock())
        monkeypatch.setitem(sys.modules, "plotly.graph_objects", MagicMock())
        return mock_st

    def test_app_exists(self):
        assert Path("dashboard/app.py").exists()

    def test_overview_page_exists(self):
        assert Path("dashboard/pages/1_overview.py").exists()

    def test_positions_page_exists(self):
        assert Path("dashboard/pages/2_positions.py").exists()

    def test_performance_page_exists(self):
        assert Path("dashboard/pages/3_performance.py").exists()

    def test_account_mgmt_page_exists(self):
        assert Path("dashboard/pages/4_account_mgmt.py").exists()
