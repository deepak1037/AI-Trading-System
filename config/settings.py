"""Single source of truth for all configuration (CLAUDE.md Section 15).

Every threshold, URL, timeout, key, and interval lives here and is loaded from
``.env`` via ``pydantic-settings``. The module validates on import — bad or
missing config raises immediately (fail fast), so nothing else in the system
ever sees an invalid setting.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.exceptions import ConfigError


class Settings(BaseSettings):
    """All runtime configuration, validated at import time."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── System ────────────────────────────────────────────────
    ENV: str = "development"  # development | backtest | paper | live
    LOG_LEVEL: str = "INFO"  # DEBUG | INFO | WARNING | ERROR | CRITICAL
    LOG_FILE: str = "logs/trading.log"
    DEBUG_SIGNALS: bool = False  # verbose signal tracing
    DRY_RUN: bool = True
    LIVE_TRADING_ENABLED: bool = False  # must be True for live orders

    # ── Broker ────────────────────────────────────────────────
    BROKER: str = "alpaca"  # alpaca | schwab | ibkr
    SCHWAB_CLIENT_ID: str = ""
    SCHWAB_CLIENT_SECRET: str = ""
    SCHWAB_REDIRECT_URI: str = ""
    SCHWAB_ACCOUNT_NUMBER: str = ""
    SCHWAB_TOKEN_FILE: str = "broker_core/tokens/schwab_token.json"
    ALPACA_API_KEY: str = ""
    ALPACA_TOKEN_FILE: str = "broker_core/tokens/alpaca_token.json"
    ALPACA_SECRET_KEY: str = ""
    ALPACA_PAPER: bool = True
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"

    # ── Signal thresholds ─────────────────────────────────────
    MACRO_SURPRISE_CRITICAL: float = 2.0  # σ
    MACRO_SURPRISE_MODERATE: float = 1.0
    YIELD_DELTA_THRESHOLD: float = 0.05  # 5 bps
    SENTIMENT_THRESHOLD: float = 0.3  # FinBERT score
    PREMARKET_FUTURES_THRESHOLD: float = -0.008  # -0.8% NQ futures
    CONFIDENCE_CRITICAL: int = 80
    CONFIDENCE_HIGH: int = 65
    SCORE_DELTA_ALERT: int = 15

    # ── Signal fusion weights (must sum to 100) ───────────────
    WEIGHT_MACRO: int = 30
    WEIGHT_YIELD: int = 25
    WEIGHT_SENTIMENT: int = 20
    WEIGHT_PREMARKET: int = 15
    WEIGHT_TECHNICAL: int = 10
    WEIGHT_TF_ALIGNMENT_BONUS: int = 10

    # ── Strategies ────────────────────────────────────────────
    ENABLED_STRATEGIES: list[str] = Field(default_factory=lambda: ["equity_long_short"])
    REGIME_CLOSE_TRIGGERS: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "strong_short": ["equity_long_short", "momentum_breakout"]
        }
    )

    # ── Risk ──────────────────────────────────────────────────
    MAX_POSITION_PCT: float = 0.05  # 5% of portfolio per position
    MAX_PORTFOLIO_RISK_PCT: float = 0.20  # 20% total at risk
    DAILY_LOSS_LIMIT_PCT: float = 0.02  # halt if down 2% on the day
    KELLY_FRACTION: float = 0.25  # use 25% of full Kelly criterion
    STOP_LOSS_ATR_MULTIPLIER: float = 2.0
    TAKE_PROFIT_ATR_MULTIPLIER: float = 3.0

    # ── Position watcher ──────────────────────────────────────
    POSITION_SCAN_INTERVAL_SECONDS: int = 60

    # ── Watcher schedule ──────────────────────────────────────
    WATCHER_OVERNIGHT_INTERVAL: int = 15  # minutes
    WATCHER_PREMARKET_INTERVAL: int = 5  # minutes
    WATCHER_MACRO_INTERVAL: int = 30  # seconds
    WATCHER_OPEN_INTERVAL: int = 1  # minutes
    WATCHER_SESSION_INTERVAL: int = 5  # minutes
    WATCHER_POWER_HOUR_INTERVAL: int = 1  # minutes

    # ── Paper trading ──────────────────────────────────────────
    PAPER_ACCOUNTS: list[str] = Field(default_factory=lambda: ["paper_main"])
    PAPER_FILL_METHOD: str = "next_open"  # next_open | vwap | worst_case
    PAPER_SLIPPAGE_PCT: float = 0.001  # 0.1% slippage on equity
    PAPER_COMMISSION_PER_SHARE: float = 0.0  # Schwab = 0 for equities
    PAPER_OPTIONS_SLIPPAGE: float = 0.5  # 50% of bid-ask spread
    PAPER_OPTIONS_COMMISSION: float = 0.65  # per contract
    PAPER_BACKUP_DAYS: int = 30
    PAPER_ACCOUNTS_DIR: str = "paper_trading/accounts"
    PAPER_TRADES_DIR: str = "paper_trading/trades"
    PAPER_BACKUPS_DIR: str = "paper_trading/backups"

    # ── Dashboard ─────────────────────────────────────────────
    DASHBOARD_REFRESH_SECONDS: int = 60
    DASHBOARD_PORT: int = 8501
    DASHBOARD_BENCHMARK_TICKER: str = "SPY"

    # ── Database ──────────────────────────────────────────────
    DB_PATH: str = "db/trading.db"

    # ── APIs ──────────────────────────────────────────────────
    API_MAX_RETRIES: int = 3
    API_BACKOFF_SECONDS: float = 2.0
    API_CIRCUIT_BREAKER_FAILURES: int = 5
    API_CIRCUIT_BREAKER_TIMEOUT: int = 300  # seconds
    POLYGON_API_KEY: str = ""
    FRED_API_KEY: str = ""
    NEWS_API_KEY: str = ""
    FMP_API_KEY: str = ""  # Financial Modeling Prep — Stage 3 fundamentals
    # Moomoo OpenD (local gateway) — primary Stage 3 fundamentals source.
    MOOMOO_HOST: str = "127.0.0.1"
    MOOMOO_PORT: int = 11111
    # OpenD financial-statement quota is 30 requests / 30s (~1/s). Pace at 1.1s
    # to stay safely under it across a large universe.
    MOOMOO_PACE_SECONDS: float = 1.1

    # ── LLM (Claude) — options ROI analysis ───────────────────
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-sonnet-4-6"
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_SECONDS: float = 60.0

    # ── Options ROI ───────────────────────────────────────────
    OPTIONS_CONTRACT_MULTIPLIER: int = 100  # shares per contract
    OPTIONS_MARGIN_BASIS: str = "reg_t"  # reg_t | cash_secured
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    TWILIO_TO_NUMBER: str = ""
    SLACK_WEBHOOK_URL: str = ""
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = ""
    SENDGRID_TO_EMAIL: str = ""
    # Required by SEC EDGAR EFTS: "Name email@example.com"
    EDGAR_IDENTITY: str = "AI Trading System trading@example.com"
    # Calls per second to stay within each provider's free-tier rate limit
    YFINANCE_RATE_LIMIT: float = 1.0
    ALPACA_RATE_LIMIT: float = 5.0
    FRED_RATE_LIMIT: float = 2.0

    # ── Alerts ────────────────────────────────────────────────
    ALERT_DEDUP_MINUTES: int = 30
    ALERT_DIGEST_INTERVAL_MINUTES: int = 60

    # ── Scanner ───────────────────────────────────────────────
    SCANNER_UNIVERSE_MIN_PRICE: float = 5.0
    SCANNER_UNIVERSE_MIN_VOLUME: int = 500_000
    SCANNER_UNIVERSE_MIN_MKTCAP: int = 200_000_000
    SCANNER_WATCHLIST_THRESHOLD: int = 65  # min composite score to enter watchlist
    SCANNER_FULL_RUN_DAY: str = "sunday"
    SCANNER_DAILY_RUN_HOUR: int = 6  # 6 AM ET
    # Composite scoring weights for the multi-bagger scanner (must sum to 100).
    # Distinct from the signal-fusion WEIGHT_* fields above (live trader).
    SCANNER_WEIGHT_FUNDAMENTAL: int = 35
    SCANNER_WEIGHT_INSTITUTIONAL: int = 25
    SCANNER_WEIGHT_TECHNICAL: int = 30
    SCANNER_WEIGHT_SQUEEZE: int = 5
    SCANNER_WEIGHT_TF_ALIGNMENT: int = 5
    # Sleep between per-ticker yfinance calls in Stages 3-5 (seconds). 0 = no
    # pacing. Set >0 (e.g. 0.6) to stay under yfinance's burst rate limit on
    # large universes, so the later stages aren't starved of data.
    SCANNER_YF_PACE_SECONDS: float = 0.0

    # ── Validators ────────────────────────────────────────────
    _VALID_ENVS: ClassVar[set[str]] = {"development", "backtest", "paper", "live"}
    _VALID_LOG_LEVELS: ClassVar[set[str]] = {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }
    _VALID_FILL_METHODS: ClassVar[set[str]] = {"next_open", "vwap", "worst_case"}
    _VALID_BROKERS: ClassVar[set[str]] = {"alpaca", "schwab", "ibkr"}
    _VALID_MARGIN_BASES: ClassVar[set[str]] = {"reg_t", "cash_secured"}

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        """Cross-field validation. Raises ConfigError on any violation."""
        if self.ENV not in self._VALID_ENVS:
            raise ConfigError(
                "Invalid ENV", value=self.ENV, allowed=sorted(self._VALID_ENVS)
            )
        if self.LOG_LEVEL not in self._VALID_LOG_LEVELS:
            raise ConfigError(
                "Invalid LOG_LEVEL",
                value=self.LOG_LEVEL,
                allowed=sorted(self._VALID_LOG_LEVELS),
            )
        if self.BROKER not in self._VALID_BROKERS:
            raise ConfigError(
                "Invalid BROKER",
                value=self.BROKER,
                allowed=sorted(self._VALID_BROKERS),
            )
        if self.PAPER_FILL_METHOD not in self._VALID_FILL_METHODS:
            raise ConfigError(
                "Invalid PAPER_FILL_METHOD",
                value=self.PAPER_FILL_METHOD,
                allowed=sorted(self._VALID_FILL_METHODS),
            )
        if self.OPTIONS_MARGIN_BASIS not in self._VALID_MARGIN_BASES:
            raise ConfigError(
                "Invalid OPTIONS_MARGIN_BASIS",
                value=self.OPTIONS_MARGIN_BASIS,
                allowed=sorted(self._VALID_MARGIN_BASES),
            )

        # Signal fusion weights must sum to 100 (the tf-alignment bonus is a
        # separate additive bonus, not part of the base ensemble — Section 6).
        weight_sum = (
            self.WEIGHT_MACRO
            + self.WEIGHT_YIELD
            + self.WEIGHT_SENTIMENT
            + self.WEIGHT_PREMARKET
            + self.WEIGHT_TECHNICAL
        )
        if weight_sum != 100:
            raise ConfigError(
                "Signal fusion weights must sum to 100", actual_sum=weight_sum
            )

        scanner_weight_sum = (
            self.SCANNER_WEIGHT_FUNDAMENTAL
            + self.SCANNER_WEIGHT_INSTITUTIONAL
            + self.SCANNER_WEIGHT_TECHNICAL
            + self.SCANNER_WEIGHT_SQUEEZE
            + self.SCANNER_WEIGHT_TF_ALIGNMENT
        )
        if scanner_weight_sum != 100:
            raise ConfigError(
                "Scanner scoring weights must sum to 100", actual_sum=scanner_weight_sum
            )

        # The live triple-gate must be internally consistent (Section 2).
        if self.LIVE_TRADING_ENABLED and self.DRY_RUN:
            raise ConfigError(
                "LIVE_TRADING_ENABLED=True is incompatible with DRY_RUN=True"
            )
        if self.ENV == "live" and (self.DRY_RUN or not self.LIVE_TRADING_ENABLED):
            raise ConfigError(
                "ENV=live requires DRY_RUN=False and LIVE_TRADING_ENABLED=True",
                dry_run=self.DRY_RUN,
                live_trading_enabled=self.LIVE_TRADING_ENABLED,
            )

        return self


def _load_settings() -> Settings:
    """Instantiate Settings, re-wrapping pydantic errors as ConfigError."""
    try:
        return Settings()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Failed to load settings: {exc}") from exc


# Validated at import — bad config = fail fast.
settings = _load_settings()


__all__ = ["Settings", "settings"]
