"""SQLite initialisation and connection management (CLAUDE.md Section 16).

All tables are created with ``IF NOT EXISTS`` so ``init_db()`` is safe to call
repeatedly (idempotent). Always use ``get_connection()`` — never hold raw
sqlite3.Connection objects outside of a ``with`` block.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# All DDL from CLAUDE.md Section 16.  IF NOT EXISTS makes it idempotent.
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    direction       TEXT    NOT NULL,
    confidence      INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    metadata_json   TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_state_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    regime          TEXT    NOT NULL,
    composite_score INTEGER NOT NULL,
    previous_regime TEXT,
    timestamp       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    channel   TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accuracy_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id           INTEGER REFERENCES signals(id),
    predicted_direction TEXT    NOT NULL,
    actual_direction    TEXT,
    actual_pct_move     REAL,
    was_correct         INTEGER,
    date                TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    position_type TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    qty           INTEGER NOT NULL,
    fill_price    REAL    NOT NULL,
    commission    REAL    DEFAULT 0.0,
    signal_id     INTEGER REFERENCES signals(id),
    order_id      TEXT,
    env           TEXT    NOT NULL,
    timestamp     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
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
    is_open       INTEGER DEFAULT 1,
    -- Phase 2: three-bucket framework metadata (see _POSITION_MIGRATIONS).
    bucket                INTEGER DEFAULT 1,    -- 1=MSP/Wheel 2=Earnings 3=Event/LEAP
    sub_type              TEXT    DEFAULT 'msp',
    recoverable           INTEGER DEFAULT 1,    -- 1=can roll/recover, 0=defined loss
    entry_thesis          TEXT,
    thesis_status         TEXT    DEFAULT 'active',  -- active|partial|complete|broken
    thesis_completion_pct REAL    DEFAULT 0.0,
    exit_target_pct       REAL,
    stop_loss_pct         REAL,
    last_exit_review      TEXT,
    exit_recommendation   TEXT     -- FULL_EXIT|ROLL_UP|LADDER|HOLD
);

CREATE TABLE IF NOT EXISTS watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL UNIQUE,
    name            TEXT,
    composite_score INTEGER,
    short_tf        INTEGER DEFAULT 0,
    mid_tf          INTEGER DEFAULT 0,
    long_tf         INTEGER DEFAULT 0,
    added_at        TEXT    NOT NULL,
    removed_at      TEXT,
    is_active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scanner_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage       INTEGER NOT NULL,
    tickers_in  INTEGER,
    tickers_out INTEGER,
    run_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlcv_cache (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals_cache (
    ticker     TEXT PRIMARY KEY,
    data_json  TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bucket_performance (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,
    bucket             INTEGER NOT NULL,
    sub_type           TEXT,
    realized_pnl       REAL    DEFAULT 0.0,
    unrealized_pnl     REAL    DEFAULT 0.0,
    premium_collected  REAL    DEFAULT 0.0,
    positions_opened   INTEGER DEFAULT 0,
    positions_closed   INTEGER DEFAULT 0,
    win_count          INTEGER DEFAULT 0,
    loss_count         INTEGER DEFAULT 0,
    created_at         TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wheel_cycles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT    NOT NULL,
    phase              TEXT    NOT NULL,
    -- sell_put | assigned | sell_call | called_away | restart
    phase_entry_date   TEXT,
    phase_exit_date    TEXT,
    strike             REAL,
    shares             INTEGER DEFAULT 100,
    premium_collected  REAL    DEFAULT 0.0,
    cost_basis         REAL,
    total_cycle_return REAL    DEFAULT 0.0,
    cycle_number       INTEGER DEFAULT 1,
    notes              TEXT,
    created_at         TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS exit_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER,
    ticker        TEXT    NOT NULL,
    review_date   TEXT    NOT NULL,
    profit_pct    REAL,
    dte_remaining INTEGER,
    thesis_status TEXT,
    macro_regime  TEXT,
    recommendation TEXT,
    -- FULL_EXIT | ROLL_UP | LADDER | HOLD
    confidence    INTEGER,
    reasoning     TEXT,
    suggested_action TEXT,
    -- JSON: {sell: ..., buy: ..., net_credit: ...}
    executed      INTEGER DEFAULT 0,
    created_at    TEXT    DEFAULT (datetime('now'))
);
"""

# Phase 2 columns added to an already-existing positions table. Applied by
# init_db() via ALTER TABLE when missing (idempotent, never breaks old data).
_POSITION_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("bucket", "INTEGER DEFAULT 1"),
    ("sub_type", "TEXT DEFAULT 'msp'"),
    ("recoverable", "INTEGER DEFAULT 1"),
    ("entry_thesis", "TEXT"),
    ("thesis_status", "TEXT DEFAULT 'active'"),
    ("thesis_completion_pct", "REAL DEFAULT 0.0"),
    ("exit_target_pct", "REAL"),
    ("stop_loss_pct", "REAL"),
    ("last_exit_review", "TEXT"),
    ("exit_recommendation", "TEXT"),
)

_EXPECTED_TABLES = frozenset(
    {
        "signals",
        "market_state_log",
        "alerts_log",
        "accuracy_log",
        "trades",
        "positions",
        "watchlist",
        "scanner_runs",
        "ohlcv_cache",
        "fundamentals_cache",
        "bucket_performance",
        "wheel_cycles",
        "exit_reviews",
    }
)


@contextmanager
def get_connection(
    db_path: str | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Yield a configured SQLite connection.

    Commits on success, rolls back on any exception, and always closes.
    WAL journal mode allows multiple concurrent readers while a writer runs.
    ``row_factory = sqlite3.Row`` lets you access columns by name.

    Args:
        db_path: Explicit path; falls back to ``settings.DB_PATH``.
    """
    path = db_path or settings.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Create all tables (idempotent — safe to call on every startup).

    Args:
        db_path: Explicit path; falls back to ``settings.DB_PATH``.
    """
    path = db_path or settings.DB_PATH
    with get_connection(path) as conn:
        conn.executescript(_SCHEMA_SQL)
        _migrate_positions(conn)
    logger.info("Database initialised at %s", path)


def _migrate_positions(conn: sqlite3.Connection) -> None:
    """Add any missing Phase 2 columns to an existing positions table.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so we diff against the live
    schema via ``PRAGMA table_info`` and ALTER only what's absent. Idempotent.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
    for column, ddl in _POSITION_MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {column} {ddl}")
            logger.info("Migrated positions: added column %s", column)


def list_tables(db_path: str | None = None) -> set[str]:
    """Return the set of table names currently in the database."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {row["name"] for row in rows}


EXPECTED_TABLES = _EXPECTED_TABLES

__all__ = [
    "get_connection",
    "init_db",
    "list_tables",
    "EXPECTED_TABLES",
    "_migrate_positions",
]
