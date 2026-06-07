## 15. COMPLETE SETTINGS REFERENCE

File: `config/settings.py` — pydantic-settings BaseSettings class.

```python
# ── System ────────────────────────────────────────────────
ENV                          : str   = "development"   # development | backtest | paper | live
LOG_LEVEL                    : str   = "INFO"          # DEBUG | INFO | WARNING | ERROR | CRITICAL
LOG_FILE                     : str   = "logs/trading.log"
DEBUG_SIGNALS                : bool  = False            # verbose signal tracing
DRY_RUN                      : bool  = True
LIVE_TRADING_ENABLED         : bool  = False            # must be True for live orders

# ── Broker ────────────────────────────────────────────────
BROKER                       : str   = "alpaca"         # alpaca | schwab | ibkr
SCHWAB_CLIENT_ID             : str   = ""
SCHWAB_CLIENT_SECRET         : str   = ""
SCHWAB_REDIRECT_URI          : str   = ""
SCHWAB_TOKEN_FILE            : str   = "schwab_token.json"
ALPACA_API_KEY               : str   = ""
ALPACA_SECRET_KEY            : str   = ""
ALPACA_PAPER                 : bool  = True
ALPACA_BASE_URL              : str   = "https://paper-api.alpaca.markets"

# ── Signal thresholds ─────────────────────────────────────
MACRO_SURPRISE_CRITICAL      : float = 2.0             # σ
MACRO_SURPRISE_MODERATE      : float = 1.0
YIELD_DELTA_THRESHOLD        : float = 0.05            # 5 bps
SENTIMENT_THRESHOLD          : float = 0.3             # FinBERT score
PREMARKET_FUTURES_THRESHOLD  : float = -0.008          # -0.8% NQ futures
CONFIDENCE_CRITICAL          : int   = 80
CONFIDENCE_HIGH              : int   = 65
SCORE_DELTA_ALERT            : int   = 15

# ── Signal fusion weights (must sum to 100) ───────────────
WEIGHT_MACRO                 : int   = 30
WEIGHT_YIELD                 : int   = 25
WEIGHT_SENTIMENT             : int   = 20
WEIGHT_PREMARKET             : int   = 15
WEIGHT_TECHNICAL             : int   = 10
WEIGHT_TF_ALIGNMENT_BONUS    : int   = 10

# ── Strategies ────────────────────────────────────────────
ENABLED_STRATEGIES           : list  = ["equity_long_short"]
REGIME_CLOSE_TRIGGERS        : dict  = {"strong_short": ["equity_long_short", "momentum_breakout"]}

# ── Risk ──────────────────────────────────────────────────
MAX_POSITION_PCT             : float = 0.05            # 5% of portfolio per position
MAX_PORTFOLIO_RISK_PCT       : float = 0.20            # 20% total at risk
DAILY_LOSS_LIMIT_PCT         : float = 0.02            # halt if down 2% on the day
KELLY_FRACTION               : float = 0.25            # use 25% of full Kelly criterion
STOP_LOSS_ATR_MULTIPLIER     : float = 2.0
TAKE_PROFIT_ATR_MULTIPLIER   : float = 3.0

# ── Position watcher ──────────────────────────────────────
POSITION_SCAN_INTERVAL_SECONDS : int = 60

# ── Watcher schedule ──────────────────────────────────────
WATCHER_OVERNIGHT_INTERVAL   : int   = 15              # minutes
WATCHER_PREMARKET_INTERVAL   : int   = 5               # minutes
WATCHER_MACRO_INTERVAL       : int   = 30              # seconds
WATCHER_OPEN_INTERVAL        : int   = 1               # minutes
WATCHER_SESSION_INTERVAL     : int   = 5               # minutes
WATCHER_POWER_HOUR_INTERVAL  : int   = 1               # minutes

# ── Paper trading ──────────────────────────────────────────
PAPER_ACCOUNTS               : list  = ["paper_main"]
PAPER_FILL_METHOD            : str   = "next_open"     # next_open | vwap | worst_case
PAPER_SLIPPAGE_PCT           : float = 0.001           # 0.1% slippage on equity
PAPER_COMMISSION_PER_SHARE   : float = 0.0             # Schwab = 0 for equities
PAPER_OPTIONS_SLIPPAGE       : float = 0.5             # 50% of bid-ask spread
PAPER_OPTIONS_COMMISSION     : float = 0.65            # per contract
PAPER_BACKUP_DAYS            : int   = 30
PAPER_ACCOUNTS_DIR           : str   = "paper_trading/accounts"
PAPER_TRADES_DIR             : str   = "paper_trading/trades"
PAPER_BACKUPS_DIR            : str   = "paper_trading/backups"

# ── Dashboard ─────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS    : int   = 60
DASHBOARD_PORT               : int   = 8501
DASHBOARD_BENCHMARK_TICKER   : str   = "SPY"

# ── Database ──────────────────────────────────────────────
DB_PATH                      : str   = "db/trading.db"

# ── APIs ──────────────────────────────────────────────────
API_MAX_RETRIES              : int   = 3
API_BACKOFF_SECONDS          : float = 2.0
API_CIRCUIT_BREAKER_FAILURES : int   = 5
API_CIRCUIT_BREAKER_TIMEOUT  : int   = 300             # seconds
POLYGON_API_KEY              : str   = ""
FRED_API_KEY                 : str   = ""
NEWS_API_KEY                 : str   = ""
TWILIO_ACCOUNT_SID           : str   = ""
TWILIO_AUTH_TOKEN            : str   = ""
TWILIO_FROM_NUMBER           : str   = ""
TWILIO_TO_NUMBER             : str   = ""
SLACK_WEBHOOK_URL            : str   = ""
SENDGRID_API_KEY             : str   = ""
SENDGRID_FROM_EMAIL          : str   = ""
SENDGRID_TO_EMAIL            : str   = ""

# ── Alerts ────────────────────────────────────────────────
ALERT_DEDUP_MINUTES          : int   = 30
ALERT_DIGEST_INTERVAL_MINUTES: int   = 60

# ── Scanner ───────────────────────────────────────────────
SCANNER_UNIVERSE_MIN_PRICE   : float = 5.0
SCANNER_UNIVERSE_MIN_VOLUME  : int   = 500_000
SCANNER_UNIVERSE_MIN_MKTCAP  : int   = 200_000_000
SCANNER_WATCHLIST_THRESHOLD  : int   = 65              # min composite score to enter watchlist
SCANNER_FULL_RUN_DAY         : str   = "sunday"
SCANNER_DAILY_RUN_HOUR       : int   = 6               # 6 AM ET

class Config:
    env_file = ".env"
    env_file_encoding = "utf-8"

settings = Settings()  # validated at import — bad config = fail fast
```

---