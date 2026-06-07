"""Tests for data.db — SQLite schema init and connection management."""

from __future__ import annotations

import pytest

from data.db import EXPECTED_TABLES, get_connection, init_db, list_tables


def test_init_db_creates_all_expected_tables(tmp_path: pytest.TempPathFactory) -> None:
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    assert EXPECTED_TABLES.issubset(list_tables(db_path))


def test_init_db_is_idempotent(tmp_path: pytest.TempPathFactory) -> None:
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    init_db(db_path)  # second call must not raise
    assert EXPECTED_TABLES.issubset(list_tables(db_path))


def test_get_connection_commits_on_clean_exit(tmp_db: str) -> None:
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO watchlist (ticker, added_at) VALUES ('AAPL', '2024-01-01')"
        )
    # Verify persisted after context exit.
    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT ticker FROM watchlist WHERE ticker = 'AAPL'"
        ).fetchone()
    assert row is not None
    assert row["ticker"] == "AAPL"


def test_get_connection_rolls_back_on_exception(tmp_db: str) -> None:
    with pytest.raises(RuntimeError), get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO watchlist (ticker, added_at) VALUES ('GOOG', '2024-01-01')"
        )
        raise RuntimeError("simulated mid-transaction error")

    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT ticker FROM watchlist WHERE ticker = 'GOOG'"
        ).fetchone()
    assert row is None


def test_get_connection_row_factory_allows_column_access_by_name(tmp_db: str) -> None:
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO watchlist (ticker, composite_score, added_at) "
            "VALUES ('MSFT', 72, '2024-01-01')"
        )
    with get_connection(tmp_db) as conn:
        row = conn.execute("SELECT * FROM watchlist WHERE ticker = 'MSFT'").fetchone()
    assert row["ticker"] == "MSFT"
    assert row["composite_score"] == 72


def test_init_db_creates_parent_dirs(tmp_path: pytest.TempPathFactory) -> None:
    db_path = str(tmp_path / "nested" / "deep" / "trading.db")
    init_db(db_path)  # should not raise even though dirs don't exist
    assert EXPECTED_TABLES.issubset(list_tables(db_path))


def test_ohlcv_cache_primary_key_upserts(tmp_db: str) -> None:
    """INSERT OR REPLACE behaviour: same (ticker, date) overwrites."""
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv_cache "
            "(ticker, date, open, high, low, close, volume) "
            "VALUES ('AAPL', '2024-01-02', 180.0, 185.0, 179.0, 184.0, 50000000)"
        )
    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ohlcv_cache "
            "(ticker, date, open, high, low, close, volume) "
            "VALUES ('AAPL', '2024-01-02', 181.0, 186.0, 180.0, 185.0, 51000000)"
        )
    with get_connection(tmp_db) as conn:
        rows = conn.execute(
            "SELECT * FROM ohlcv_cache WHERE ticker = 'AAPL' AND date = '2024-01-02'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["close"] == pytest.approx(185.0)
