"""Tests for broker_client/buckets — classification, capacity, P&L (Step 2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from broker_client.buckets.bucket_manager import BucketManager
from broker_client.buckets.models import BucketPosition
from data.db import get_connection, init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "buckets.db")
    init_db(path)
    return path


@pytest.fixture
def mgr(db):
    return BucketManager(db_path=db)


def _insert_position(db, **kw):
    cols = {
        "account_id": "paper_main",
        "ticker": "AAPL",
        "strategy": "cash_secured_put",
        "position_type": "options_short",
        "qty": 1,
        "entry_price": 1.0,
        "opened_at": "2026-06-01",
        "is_open": 1,
        "bucket": 1,
    }
    cols.update(kw)
    keys = ", ".join(cols)
    qs = ", ".join("?" for _ in cols)
    with get_connection(db) as conn:
        conn.execute(f"INSERT INTO positions ({keys}) VALUES ({qs})", tuple(cols.values()))


# ── classification ───────────────────────────────────────────────────────────
class TestClassification:
    def test_classify_msp_position(self, mgr):
        cls = mgr.classify_position({"strategy": "cash_secured_put"})
        assert cls.bucket == 1
        assert cls.sub_type == "msp"
        assert cls.recoverable is True

    def test_classify_margin_secured_put(self, mgr):
        cls = mgr.classify_position({"strategy": "margin_secured_put"})
        assert cls.bucket == 1
        assert cls.recoverable is True

    def test_classify_covered_call_is_wheel_call(self, mgr):
        cls = mgr.classify_position({"strategy": "covered_call"})
        assert cls.bucket == 1
        assert cls.sub_type == "wheel_call"

    def test_classify_earnings_put(self, mgr):
        cls = mgr.classify_position({"strategy": "earnings_put"})
        assert cls.bucket == 2
        assert cls.sub_type == "earnings_put"
        assert cls.recoverable is True

    def test_classify_iron_condor_defined_risk(self, mgr):
        cls = mgr.classify_position({"strategy": "iron_condor"})
        assert cls.bucket == 2
        assert cls.sub_type == "earnings_spread"
        assert cls.recoverable is False

    @pytest.mark.parametrize("strat", ["call_spread", "put_spread", "straddle", "strangle"])
    def test_classify_other_spreads_2b(self, mgr, strat):
        cls = mgr.classify_position({"strategy": strat})
        assert cls.bucket == 2
        assert cls.recoverable is False

    def test_classify_leap_by_dte(self, mgr):
        cls = mgr.classify_position({"strategy": "long_call", "dte_remaining": 400})
        assert cls.bucket == 3
        assert cls.sub_type == "leap"
        assert cls.recoverable is False

    def test_classify_event_call_short_dte(self, mgr):
        cls = mgr.classify_position({"strategy": "long_call", "dte_remaining": 30})
        assert cls.bucket == 3
        assert cls.sub_type == "event_call"

    def test_classify_event_put_short_dte(self, mgr):
        cls = mgr.classify_position({"strategy": "long_put", "dte_remaining": 30})
        assert cls.bucket == 3
        assert cls.sub_type == "event_put"

    def test_classify_accepts_object(self, mgr):
        pos = SimpleNamespace(strategy="iron_condor", dte_remaining=20, option_type=None)
        cls = mgr.classify_position(pos)
        assert cls.bucket == 2

    def test_unknown_strategy_defaults_bucket1(self, mgr):
        cls = mgr.classify_position({"strategy": "mystery_play"})
        assert cls.bucket == 1
        assert cls.recoverable is True


# ── capacity ─────────────────────────────────────────────────────────────────
class TestCapacity:
    def test_capacity_respects_allocation(self, db):
        acct = SimpleNamespace(get_state=lambda: SimpleNamespace(equity=100_000.0))
        mgr = BucketManager(db_path=db, paper_account=acct)
        # Bucket 1 allocation default = 70% → $70k. Fits a $50k add.
        assert mgr.check_bucket_capacity(1, 50_000) is True
        # An $80k add exceeds the $70k budget.
        assert mgr.check_bucket_capacity(1, 80_000) is False

    def test_capacity_counts_existing_exposure(self, db):
        acct = SimpleNamespace(get_state=lambda: SimpleNamespace(equity=10_000.0))
        mgr = BucketManager(db_path=db, paper_account=acct)
        # Bucket 3 allocation default = 10% → $1,000. Put $900 of exposure in.
        _insert_position(db, bucket=3, entry_price=9.0, qty=100, strategy="long_call")
        assert mgr.check_bucket_capacity(3, 50) is True    # 900 + 50 <= 1000
        assert mgr.check_bucket_capacity(3, 200) is False  # 900 + 200 > 1000

    def test_capacity_without_equity_allows(self, mgr):
        # No paper account wired → cannot enforce, returns True.
        assert mgr.check_bucket_capacity(1, 1_000_000) is True


# ── P&L ──────────────────────────────────────────────────────────────────────
class TestBucketPnL:
    def test_pnl_open_count(self, mgr, db):
        _insert_position(db, bucket=1, ticker="AAPL")
        _insert_position(db, bucket=1, ticker="MSFT")
        pnl = mgr.get_bucket_pnl(1)
        assert pnl.open_positions == 2

    def test_pnl_realized_and_win_rate(self, mgr, db):
        _insert_position(db, bucket=2, ticker="A", is_open=0, realized_pnl=100.0)
        _insert_position(db, bucket=2, ticker="B", is_open=0, realized_pnl=-40.0)
        _insert_position(db, bucket=2, ticker="C", is_open=0, realized_pnl=60.0)
        pnl = mgr.get_bucket_pnl(2)
        assert pnl.realized_pnl == pytest.approx(120.0)
        assert pnl.win_rate == pytest.approx(66.7, abs=0.1)

    def test_summary_returns_all_three(self, mgr):
        summary = mgr.get_bucket_summary()
        assert set(summary) == {1, 2, 3}
        assert summary[1].bucket == 1

    def test_record_classification_persists(self, mgr, db):
        _insert_position(db, bucket=1, ticker="NVDA", strategy="long_call")
        with get_connection(db) as conn:
            pid = conn.execute("SELECT id FROM positions WHERE ticker='NVDA'").fetchone()["id"]
        cls = mgr.classify_position({"strategy": "long_call", "dte_remaining": 200})
        mgr.record_classification(pid, cls)
        with get_connection(db) as conn:
            row = conn.execute("SELECT bucket, sub_type FROM positions WHERE id=?", (pid,)).fetchone()
        assert row["bucket"] == 3
        assert row["sub_type"] == "leap"


# ── BucketPosition derived fields ────────────────────────────────────────────
class TestBucketPosition:
    def test_dte_used_pct(self):
        p = BucketPosition(ticker="X", original_dte=40, dte_remaining=10)
        assert p.dte_used_pct == pytest.approx(75.0)

    def test_dte_used_pct_zero_original(self):
        p = BucketPosition(ticker="X", original_dte=0, dte_remaining=0)
        assert p.dte_used_pct == 0.0

    def test_loss_pct(self):
        assert BucketPosition(ticker="X", profit_pct=-200.0).loss_pct == 200.0
        assert BucketPosition(ticker="X", profit_pct=50.0).loss_pct == 0.0

    def test_is_itm_put(self):
        p = BucketPosition(ticker="X", option_type="put", strike=100.0, underlying_price=95.0)
        assert p.is_itm is True

    def test_is_itm_call(self):
        p = BucketPosition(ticker="X", option_type="call", strike=100.0, underlying_price=110.0)
        assert p.is_itm is True

    def test_is_itm_equity_false(self):
        assert BucketPosition(ticker="X").is_itm is False

    def test_position_value_option_multiplier(self):
        p = BucketPosition(ticker="X", option_type="put", entry_price=2.0, qty=3)
        assert p.position_value == pytest.approx(600.0)  # 2 * 3 * 100

    def test_position_value_equity(self):
        p = BucketPosition(ticker="X", entry_price=50.0, qty=10)
        assert p.position_value == pytest.approx(500.0)
