"""Tests for Phase 2 positions migration + new bucket/wheel/exit tables."""

from __future__ import annotations

import sqlite3

from data.db import _migrate_positions, get_connection, init_db, list_tables

# The legacy (Phase 1) positions schema — no bucket columns.
_OLD_POSITIONS = """
CREATE TABLE positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    position_type TEXT    NOT NULL,
    qty           INTEGER NOT NULL,
    entry_price   REAL    NOT NULL,
    stop_loss     REAL,
    take_profit   REAL,
    opened_at     TEXT    NOT NULL,
    closed_at     TEXT,
    exit_reason   TEXT,
    realized_pnl  REAL,
    is_open       INTEGER DEFAULT 1
);
"""

_PHASE2_COLUMNS = {
    "bucket",
    "sub_type",
    "recoverable",
    "entry_thesis",
    "thesis_status",
    "thesis_completion_pct",
    "exit_target_pct",
    "stop_loss_pct",
    "last_exit_review",
    "exit_recommendation",
}


def _columns(db_path: str, table: str) -> set[str]:
    with get_connection(db_path) as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_phase2_columns(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)
    assert _PHASE2_COLUMNS.issubset(_columns(db_path, "positions"))


def test_new_phase2_tables_created(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)
    tables = list_tables(db_path)
    assert {"bucket_performance", "wheel_cycles", "exit_reviews"}.issubset(tables)


def test_migration_adds_columns_to_legacy_positions(tmp_path) -> None:
    db_path = str(tmp_path / "legacy.db")
    # Build a legacy DB with a row of real data we must not lose.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_OLD_POSITIONS)
        conn.execute(
            "INSERT INTO positions "
            "(account_id, ticker, strategy, position_type, qty, entry_price, opened_at) "
            "VALUES ('paper_main', 'AAPL', 'equity_long_short', 'equity_long', 10, 150.0, '2026-01-01')"
        )
    init_db(db_path)  # runs the migration
    cols = _columns(db_path, "positions")
    assert _PHASE2_COLUMNS.issubset(cols)
    # Existing data preserved + new columns defaulted.
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM positions WHERE ticker='AAPL'").fetchone()
    assert row["qty"] == 10
    assert row["bucket"] == 1
    assert row["sub_type"] == "msp"
    assert row["recoverable"] == 1
    assert row["thesis_status"] == "active"


def test_migration_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_OLD_POSITIONS)
    init_db(db_path)
    # Second migration must be a no-op (no duplicate-column error).
    with get_connection(db_path) as conn:
        _migrate_positions(conn)
    assert _PHASE2_COLUMNS.issubset(_columns(db_path, "positions"))


def test_bucket_performance_insert_and_read(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO bucket_performance (date, bucket, sub_type, realized_pnl) "
            "VALUES ('2026-06-12', 1, 'msp', 123.45)"
        )
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM bucket_performance").fetchone()
    assert row["bucket"] == 1
    assert row["realized_pnl"] == 123.45
    assert row["positions_opened"] == 0  # default applied


def test_wheel_cycles_insert_defaults(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO wheel_cycles (ticker, phase, strike) VALUES ('HOOD', 'sell_put', 10.0)"
        )
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM wheel_cycles").fetchone()
    assert row["shares"] == 100
    assert row["cycle_number"] == 1
    assert row["created_at"] is not None


def test_exit_reviews_insert(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO exit_reviews (ticker, review_date, recommendation, confidence) "
            "VALUES ('NVDA', '2026-06-12', 'HOLD', 72)"
        )
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM exit_reviews").fetchone()
    assert row["recommendation"] == "HOLD"
    assert row["executed"] == 0
