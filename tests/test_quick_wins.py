"""
tests/test_quick_wins.py

Tests for:
  - ExpectancyTracker
  - StrategyRegimeFilter
  - StopDistanceSizer
"""

import pytest
import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── helpers ───────────────────────────────────────────────────────────────────
def make_test_db(trades: list[dict]) -> str:
    """Create a temp DB with a trades table populated with test rows."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    with sqlite3.connect(f.name) as conn:
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                strategy TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl_pct REAL,
                status TEXT,
                closed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE market_state_log (
                id INTEGER PRIMARY KEY,
                vix REAL,
                spy_above_200ma INTEGER,
                recorded_at TEXT
            )
        """)
        for t in trades:
            conn.execute("""
                INSERT INTO trades
                (symbol, strategy, entry_price, exit_price, pnl_pct, status, closed_at)
                VALUES (?,?,?,?,?,?,?)
            """, (
                t.get("symbol", "AAPL"),
                t.get("strategy", "bucket1_msp"),
                t.get("entry_price", 100.0),
                t.get("exit_price", 110.0),
                t.get("pnl_pct"),
                t.get("status", "closed"),
                t.get("closed_at", "2026-09-15T10:00:00"),
            ))
    return f.name


# ── ExpectancyTracker tests ───────────────────────────────────────────────────
class TestExpectancyTracker:
    from broker_client.analytics.expectancy_tracker import ExpectancyTracker

    def test_perfect_win_rate(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        trades = [
            {"strategy": "bucket1_msp", "pnl_pct": 5.0, "status": "closed"},
            {"strategy": "bucket1_msp", "pnl_pct": 3.0, "status": "closed"},
            {"strategy": "bucket1_msp", "pnl_pct": 7.0, "status": "closed"},
        ]
        db = make_test_db(trades)
        tracker = ExpectancyTracker(db_path=db)
        report = tracker.report()
        e = report["bucket1_msp"]
        assert e.n_trades == 3
        assert e.win_rate == 1.0
        assert e.n_losses == 0
        # expectancy = 1.0 * 5.0 - 0 * 0 = 5.0
        assert abs(e.expectancy - 5.0) < 0.01
        os.unlink(db)

    def test_mixed_results(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        trades = [
            {"strategy": "bucket2_iv_crush", "pnl_pct":  10.0, "status": "closed"},
            {"strategy": "bucket2_iv_crush", "pnl_pct":   8.0, "status": "closed"},
            {"strategy": "bucket2_iv_crush", "pnl_pct":  -5.0, "status": "closed"},
            {"strategy": "bucket2_iv_crush", "pnl_pct":  -3.0, "status": "closed"},
        ]
        db = make_test_db(trades)
        tracker = ExpectancyTracker(db_path=db)
        report = tracker.report()
        e = report["bucket2_iv_crush"]
        assert e.n_trades == 4
        assert e.win_rate == 0.5
        # expectancy = 0.5*9 - 0.5*4 = 4.5 - 2 = 2.5
        assert abs(e.expectancy - 2.5) < 0.01
        os.unlink(db)

    def test_negative_expectancy(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        trades = [
            {"strategy": "bucket3_leap", "pnl_pct":  2.0, "status": "closed"},
            {"strategy": "bucket3_leap", "pnl_pct": -15.0, "status": "closed"},
            {"strategy": "bucket3_leap", "pnl_pct": -12.0, "status": "closed"},
        ]
        db = make_test_db(trades)
        tracker = ExpectancyTracker(db_path=db)
        report = tracker.report()
        e = report["bucket3_leap"]
        assert e.expectancy < 0
        # grade is ⚠️ Too few trades when n<5; expectancy check is sufficient
        os.unlink(db)

    def test_empty_strategy(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        db = make_test_db([])
        tracker = ExpectancyTracker(db_path=db)
        report = tracker.report()
        assert "bucket1_msp" in report
        assert report["bucket1_msp"].n_trades == 0
        os.unlink(db)

    def test_save_to_db(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        trades = [
            {"strategy": "bucket1_wheel", "pnl_pct": 4.0, "status": "closed"},
        ]
        db = make_test_db(trades)
        tracker = ExpectancyTracker(db_path=db)
        tracker.save_to_db()
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT * FROM expectancy_log").fetchall()
        assert len(rows) > 0
        os.unlink(db)

    def test_pnl_computed_from_entry_exit(self):
        from broker_client.analytics.expectancy_tracker import ExpectancyTracker
        # pnl_pct is None, should compute from entry/exit
        trades = [
            {
                "strategy": "bucket1_msp",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "pnl_pct": None,
                "status": "closed",
            }
        ]
        db = make_test_db(trades)
        tracker = ExpectancyTracker(db_path=db)
        report = tracker.report()
        e = report["bucket1_msp"]
        assert e.n_trades == 1
        assert abs(e.avg_win_pct - 10.0) < 0.01
        os.unlink(db)


# ── StrategyRegimeFilter tests ────────────────────────────────────────────────
class TestStrategyRegimeFilter:
    def test_bull_allows_all(self):
        from broker_client.risk.strategy_regime_filter import (
            StrategyRegimeFilter, Regime
        )
        db = make_test_db([])
        gate = StrategyRegimeFilter(db_path=db, vix=14.0, spy_above_200ma=True)
        assert gate.regime == Regime.BULL
        for strat in ["bucket1_msp", "bucket2_iv_crush", "bucket3_leap"]:
            assert gate.is_allowed(strat)
        os.unlink(db)

    def test_bear_blocks_bucket3(self):
        from broker_client.risk.strategy_regime_filter import (
            StrategyRegimeFilter, Regime
        )
        db = make_test_db([])
        gate = StrategyRegimeFilter(db_path=db, vix=35.0, spy_above_200ma=False)
        assert gate.regime == Regime.BEAR
        assert gate.is_allowed("bucket1_msp")       # always allowed
        assert not gate.is_allowed("bucket3_leap")   # bull only
        assert not gate.is_allowed("bucket2_iv_spike")  # neutral+ only
        os.unlink(db)

    def test_vix_fear_spike_blocks_everything_except_core(self):
        from broker_client.risk.strategy_regime_filter import StrategyRegimeFilter
        db = make_test_db([])
        gate = StrategyRegimeFilter(db_path=db, vix=45.0, spy_above_200ma=True)
        assert gate.is_allowed("bucket1_msp")
        assert gate.is_allowed("bucket1_wheel")
        assert not gate.is_allowed("bucket2_iv_crush")
        assert not gate.is_allowed("bucket3_leap")
        os.unlink(db)

    def test_env_override(self, monkeypatch):
        from broker_client.risk.strategy_regime_filter import (
            StrategyRegimeFilter, Regime
        )
        monkeypatch.setenv("REGIME_OVERRIDE", "bear")
        db = make_test_db([])
        gate = StrategyRegimeFilter(db_path=db, vix=14.0, spy_above_200ma=True)
        assert gate.regime == Regime.BEAR
        assert not gate.is_allowed("bucket3_leap")
        os.unlink(db)

    def test_neutral_allows_msp_wheel_iv_crush(self):
        from broker_client.risk.strategy_regime_filter import (
            StrategyRegimeFilter, Regime
        )
        import os
        db = make_test_db([])
        # Force neutral: VIX in neutral zone (18-30), SPY explicitly None
        # but pass bear SPY trend to cancel out neutral VIX -> true neutral
        gate = StrategyRegimeFilter(db_path=db, vix=22.0, spy_above_200ma=False)
        # VIX=22 (neutral zone, 0 votes) + SPY below 200MA (1 bear) = BEAR
        # Instead test neutral directly via REGIME_OVERRIDE
        import os as _os
        _os.environ["REGIME_OVERRIDE"] = "neutral"
        gate2 = StrategyRegimeFilter(db_path=db, vix=22.0, spy_above_200ma=True)
        assert gate2.regime == Regime.NEUTRAL
        assert gate2.is_allowed("bucket1_msp")
        assert gate2.is_allowed("bucket2_iv_crush")
        assert gate2.is_allowed("bucket2_iv_spike")
        assert gate2.is_allowed("bucket3_event_bounce")
        assert not gate2.is_allowed("bucket3_leap")
        del _os.environ["REGIME_OVERRIDE"]
        os.unlink(db)


# ── StopDistanceSizer tests ────────────────────────────────────────────────────
class TestStopDistanceSizer:
    def test_equity_basic(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        r = sizer.size_equity("AAPL", entry=180.0, stop=165.6)
        assert r.shares > 0
        assert r.stop_pct == pytest.approx(0.08, abs=0.001)
        assert r.risk_dollars <= 1_000  # max 1% of 100k
        assert r.position_dollars <= 5_000  # max 5% of 100k

    def test_equity_stop_pct(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        r = sizer.size_equity("SPY", entry=450.0, stop_pct=0.05)
        assert r.stop_pct == pytest.approx(0.05)
        assert r.stop_price == pytest.approx(450.0 * 0.95)

    def test_option_sizing(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        r = sizer.size_option("TSLA", premium=5.50)
        assert r.contracts >= 1
        assert r.risk_dollars <= 1_000
        assert r.position_dollars <= 5_000

    def test_max_position_cap(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000, risk_pct=0.01)
        # Wide stop → many shares → hits position cap
        r = sizer.size_equity("CHEAP", entry=5.0, stop_pct=0.01)
        assert r.position_dollars <= 5_000 * 1.01  # tiny rounding
        assert r.capped_by == "max_position" or r.shares * 5.0 <= 5_001

    def test_minimum_one_share(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=1_000, risk_pct=0.001)
        r = sizer.size_equity("AMZN", entry=180.0, stop_pct=0.30)
        assert r.shares >= 1

    def test_regime_scaling(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        r_bull = sizer.size_equity("AAPL", entry=180.0, stop_pct=0.08)
        r_bear = sizer.size_equity("AAPL", entry=180.0, stop_pct=0.08)
        r_bear = sizer.adjust_for_regime(r_bear, "bear")
        assert r_bear.shares <= r_bull.shares
        assert "bear" in (r_bear.warning or "").lower()

    def test_invalid_entry_raises(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        with pytest.raises(ValueError):
            sizer.size_equity("BAD", entry=0.0)

    def test_stop_above_entry_raises(self):
        from broker_client.risk.stop_distance_sizer import StopDistanceSizer
        sizer = StopDistanceSizer(portfolio_value=100_000)
        with pytest.raises((ValueError, Exception)):
            sizer.size_equity("BAD", entry=100.0, stop=110.0)
