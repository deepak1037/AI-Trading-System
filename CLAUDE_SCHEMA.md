## 16. DATABASE SCHEMA (SQLite)

File auto-created at `DB_PATH` on first run.

```sql
-- Signals log
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    source TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}'
);

-- Market state transitions log
CREATE TABLE market_state_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    regime TEXT NOT NULL,
    composite_score INTEGER NOT NULL,
    previous_regime TEXT,
    timestamp TEXT NOT NULL
);

-- Alerts sent
CREATE TABLE alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    channel TEXT NOT NULL,    -- sms | slack | email
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

-- Signal accuracy (for model retraining)
CREATE TABLE accuracy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    predicted_direction TEXT NOT NULL,
    actual_direction TEXT,    -- filled at EOD
    actual_pct_move REAL,
    was_correct INTEGER,      -- 0 | 1, filled at EOD
    date TEXT NOT NULL
);

-- Live trade log (paper + live)
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,          -- paper_main | schwab_live
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    position_type TEXT NOT NULL,       -- equity_long | equity_short | options_long | etc
    action TEXT NOT NULL,              -- BUY | SELL
    qty INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    commission REAL DEFAULT 0.0,
    signal_id INTEGER REFERENCES signals(id),
    order_id TEXT,
    env TEXT NOT NULL,                 -- backtest | paper | live
    timestamp TEXT NOT NULL
);

-- Open positions (both paper and live)
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    position_type TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_reason TEXT,
    realized_pnl REAL,
    is_open INTEGER DEFAULT 1          -- 1=open, 0=closed
);

-- Watchlist
CREATE TABLE watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT,
    composite_score INTEGER,
    short_tf INTEGER DEFAULT 0,
    mid_tf INTEGER DEFAULT 0,
    long_tf INTEGER DEFAULT 0,
    added_at TEXT NOT NULL,
    removed_at TEXT,
    is_active INTEGER DEFAULT 1
);

-- Scanner run log
CREATE TABLE scanner_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage INTEGER NOT NULL,
    tickers_in INTEGER,
    tickers_out INTEGER,
    run_at TEXT NOT NULL
);

-- Cache tables
CREATE TABLE ohlcv_cache (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE fundamentals_cache (
    ticker TEXT PRIMARY KEY,
    data_json TEXT,
    fetched_at TEXT NOT NULL
);
```

---