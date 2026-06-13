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

    # ── Sentiment freshness + geopolitical shock detection ────────────────────
    # Re-fetch headlines from NewsAPI/RSS at most once per TTL; within the TTL
    # the cached batch is re-scored (so sentiment reflects CURRENT news without
    # hammering the API every 30s tick). 900s = 15 min.
    SENTIMENT_CACHE_TTL_SECONDS: int = 900
    # NewsAPI "everything" lookback window (hours) — keeps headlines current.
    SENTIMENT_NEWS_LOOKBACK_HOURS: int = 24
    # Geopolitical / event-shock detector. If any keyword appears in current
    # headlines AND their sentiment is sharply negative, fire a risk-off shock.
    GEOPOLITICAL_KEYWORDS: list[str] = Field(
        default_factory=lambda: [
            "war", "strikes", "strike", "military", "geopolitical", "attack",
            "sanctions", "invasion", "missile", "airstrike", "nuclear",
            "Iran", "China", "Taiwan", "Russia", "Ukraine", "NATO", "Israel",
        ]
    )
    GEOPOLITICAL_SENTIMENT_DROP: float = -0.3  # avg score below this = sharp drop
    # Require a CLUSTER of geopolitical headlines, not one passing mention —
    # guards against false shocks on normal days where "China"/"war"/"strike"
    # appear in routine economic/markets headlines.
    GEOPOLITICAL_MIN_HEADLINES: int = 2
    GEOPOLITICAL_SHOCK_CONFIDENCE: int = 70    # above CONFIDENCE_HIGH → alerts
    GEOPOLITICAL_POLL_SECONDS: int = 60        # dedicated sub-minute poll cadence
    GEOPOLITICAL_WATCH: bool = True            # master switch for the detector

    # ── Presidential / Truth Social monitoring ────────────────────────────────
    # Presidential posts move markets instantly. Truth Social disables the
    # Mastodon .rss route (serves the SPA shell), so we use trumpstruth.org,
    # which mirrors his posts as a working RSS feed. Polled on the geopolitical
    # 60s cadence. A market-keyword match fires regardless of FinBERT sentiment.
    PRESIDENTIAL_WATCH: bool = True
    PRESIDENTIAL_RSS_URLS: list[str] = Field(
        default_factory=lambda: ["https://trumpstruth.org/feed"]
    )
    # Only alert on posts published within this window — react to breaking posts,
    # not the whole 100-post backlog on the first poll after startup.
    PRESIDENTIAL_FRESH_MINUTES: int = 15
    PRESIDENTIAL_SHOCK_CONFIDENCE: int = 80  # critical — routes to #alerts
    PRESIDENTIAL_POSITIVE_KEYWORDS: list[str] = Field(
        default_factory=lambda: [
            "deal", "trade deal", "agreement", "peace", "ceasefire", "truce",
            "tariff reduction", "signing", "signed",
        ]
    )
    PRESIDENTIAL_NEGATIVE_KEYWORDS: list[str] = Field(
        default_factory=lambda: [
            "strikes", "strike", "attack", "tariffs", "tariff", "sanctions",
            "blockade", "no deal", "terminated", "terminate",
        ]
    )
    # De-escalation words that FLIP a negative keyword bullish — so "cancelled
    # strikes" / "lifted sanctions" read as LONG, not SHORT (the live miss).
    PRESIDENTIAL_NEGATION_WORDS: list[str] = Field(
        default_factory=lambda: [
            "cancel", "cancelled", "canceled", "cancelling", "canceling",
            "call off", "called off", "calling off", "lifted", "lift", "lifting",
            "ended", "ending", "suspend", "suspended", "halt", "halted",
            "no longer", "reversed", "reversing", "removed", "removing",
            "averted", "avoided", "scrapped", "stand down", "stood down",
            "pulled back",
        ]
    )

    # ── Phase 2b: real-time presidential monitoring (multi-source) ────────────
    # Upgrade Truth Social detection from 60s RSS polling to near-real-time. The
    # monitor tries sources in PRESIDENTIAL_SOURCE_PRIORITY order and uses the
    # first that connects; with no API key it degrades gracefully to RSS.
    PRESIDENTIAL_TRUMP_USER_ID: str = "107780257626128497"  # Truth Social user id

    # Source A: ScrapeCreators REST API (fast polling). Free tier: 100 credits;
    # each poll = 1 credit (10s polling ≈ 8,640/day → needs a paid plan to run
    # sustained). Leave blank to skip.
    SCRAPECREATORS_API_KEY: str = ""
    SCRAPECREATORS_POLL_SECONDS: int = 10

    # Source B: TweetStream WebSocket (true real-time push).
    TWEETSTREAM_API_KEY: str = ""
    TWEETSTREAM_WS_URL: str = "wss://api.tweetstream.io/v1/stream"

    # Source C: Apify actor (REST with webhook support).
    APIFY_API_KEY: str = ""
    APIFY_ACTOR_ID: str = "muhammetakkurtt/truth-social-scraper"

    # Source priority — the monitor tries these in order, first to connect wins.
    PRESIDENTIAL_SOURCE_PRIORITY: list[str] = Field(
        default_factory=lambda: ["scrapecreators", "tweetstream", "apify", "rss"]
    )
    # Don't re-alert the same post within this window.
    PRESIDENTIAL_DEDUP_MINUTES: int = 60
    # Market-direction keyword sets for the real-time monitor's classifier.
    # Longest match wins (so "tariff reduction"/"cancelled strikes" read LONG).
    PRESIDENTIAL_MARKET_KEYWORDS_LONG: list[str] = Field(
        default_factory=lambda: [
            "deal", "agreement", "peace", "cancelled strikes",
            "ceasefire", "tariff reduction", "trade deal",
            "signing", "lifted sanctions", "no tariff",
            "extension", "pause tariffs", "market open",
        ]
    )
    PRESIDENTIAL_MARKET_KEYWORDS_SHORT: list[str] = Field(
        default_factory=lambda: [
            "tariff", "sanction", "strike", "attack",
            "invasion", "blockade", "no deal", "terminated",
            "maximum pressure", "military action",
            "deploying troops", "war", "bombs",
        ]
    )

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

    # ── Three-bucket framework (Phase 2) ──────────────────────
    # Capital is split across three risk buckets. Allocations must sum to <= 100;
    # any remainder is the cash buffer kept for rolls/assignments.
    TRADING_PROFILE: str = "moderate"  # conservative | moderate | aggressive | custom
    BUCKET1_ALLOCATION_PCT: int = 70   # Margin Secured Put + Wheel (income engine)
    BUCKET2_ALLOCATION_PCT: int = 20   # Earnings plays
    BUCKET3_ALLOCATION_PCT: int = 10   # Event + LEAP (lotto) plays
    # Per-trade limits.
    BUCKET3_MAX_SINGLE_TRADE_PCT: float = 2.0   # max % of portfolio on one B3 play
    BUCKET2_DEFINED_RISK_MAX_LOSS_MULTIPLE: float = 2.0  # close B2B at 2x premium
    # Exit-rule profit/loss thresholds (percent), never hardcoded in the engine.
    BUCKET1_PROFIT_TARGET_PCT: float = 50.0      # take profit at 50%
    BUCKET1_FAST_PROFIT_PCT: float = 70.0        # 70-80% near expiry → exit
    BUCKET1_EARLY_PROFIT_PCT: float = 25.0       # 25% in week 1 on long DTE → exit
    BUCKET2B_PROFIT_TARGET_PCT: float = 50.0     # defined-risk take profit
    BUCKET2B_STOP_LOSS_PCT: float = 200.0        # lost 2x premium → stop loss
    BUCKET3_LLM_REVIEW_PROFIT_PCT: float = 40.0  # trigger LLM review at 40%
    BUCKET3_STOP_LOSS_PCT: float = 100.0         # full premium lost
    DTE_EXIT_THRESHOLD: int = 21                 # the 21-DTE rule
    DTE_URGENT_THRESHOLD: int = 7                # 7-DTE urgent alert
    DTE_SHORT_MAX: int = 14                      # short-DTE tier upper bound
    DTE_MEDIUM_MAX: int = 44                     # medium-DTE tier upper bound
    DTE_USED_PCT_REVIEW: float = 50.0            # B3: half the bought time used
    # Scale-out (ladder) execution — close in tranches (must sum to 1.0).
    LADDER_DEFAULT_TRANCHES: list[float] = Field(
        default_factory=lambda: [0.3, 0.4, 0.3]
    )
    LADDER_SECOND_TARGET_PROFIT_PCT: float = 70.0  # 2nd tranche defers to this profit
    LADDER_TRAILING_STOP_PCT: float = 0.0          # final tranche rides to breakeven

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

    # ── BLS (Bureau of Labor Statistics) — live CPI at release time ───────────
    # FRED lags the BLS release by 15-30 min; BLS's own API reflects the print
    # at 8:30 AM ET. We poll it directly during the macro phase and fall back to
    # FRED only if BLS fails. No key needed for basic access (25 queries/day
    # unregistered); a free registration key raises the limit to 500/day.
    BLS_API_URL: str = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    BLS_API_KEY: str = ""  # optional registration key (higher rate limit)
    # Headline CPI reported YoY is computed from the NON-seasonally-adjusted
    # index (CUUR0000SA0), NOT the SA index (CUSR0000SA0). Core = ...SA0L1E.
    CPI_SERIES_ID: str = "CUUR0000SA0"          # CPI-U, all items, NSA (YoY base)
    CORE_CPI_SERIES_ID: str = "CUUR0000SA0L1E"  # all items less food & energy, NSA
    # Today's analyst consensus, in YoY percent. Update each release.
    CPI_CONSENSUS_YOY: float = 4.2
    CORE_CPI_CONSENSUS_YOY: float = 2.9
    CPI_RELEASE_LABEL: str = "May 2026"  # which month this consensus is for (metadata/logs)
    # Master switch: poll BLS CPI live during the 08:15-09:30 ET macro phase.
    MACRO_CPI_WATCH: bool = True
    # Fallback surprise sigma (YoY percentage points) when too little BLS history.
    CPI_YOY_FALLBACK_STD: float = 0.2
    # Cache a BLS reading this long so the 30s macro tick polls BLS at most once
    # per window — the anonymous BLS limit is only 25 queries/day. 300s = 5 min.
    BLS_CACHE_TTL_SECONDS: int = 300
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

    # ── Discord webhooks ──────────────────────────────────────
    DISCORD_ENABLED: bool = True
    DISCORD_WEBHOOK_ALERTS: str = ""         # #alerts — critical (red)
    DISCORD_WEBHOOK_SIGNALS: str = ""        # #signals — high (yellow)
    DISCORD_WEBHOOK_OPPORTUNITIES: str = ""  # #opportunities — green
    DISCORD_WEBHOOK_BRIEFING: str = ""       # #daily-briefing — blue

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

    # ── Earnings analyzer (Phase 3, Module A) ─────────────────
    EARNINGS_DAYS_AHEAD: int = 14
    EARNINGS_IV_RANK_SELL_THRESHOLD: int = 50  # IV elevated → sell premium (crush)
    EARNINGS_IV_RANK_BUY_THRESHOLD: int = 35   # IV low → buy premium (spike)
    EARNINGS_IV_PERCENTILE_SELL_THRESHOLD: int = 60
    EARNINGS_IV_PERCENTILE_BUY_THRESHOLD: int = 40
    EARNINGS_MIN_IV_CRUSH_HISTORY: int = 6     # min quarters of data to trust pattern
    EARNINGS_MIN_IV_CRUSH_PCT: float = 15.0    # avg crush to be a "reliable crusher"
    EARNINGS_IV_CRUSH_MEANINGFUL_PCT: float = 10.0  # per-quarter crush counted as real
    EARNINGS_IV_CRUSH_CONSISTENCY: float = 0.6      # frac of quarters that must crush
    EARNINGS_MAX_BREACH_RATE: float = 0.25     # max breach for a crush (sell) trade
    EARNINGS_MIN_BREACH_RATE: float = 0.40     # min breach for a spike (buy) trade
    EARNINGS_MIN_ACTUAL_MOVE_FOR_SPIKE: float = 8.0  # avg move % needed for a spike buy
    EARNINGS_SAFETY_BUFFER: float = 1.1        # 10% extra OTM buffer on put strike
    EARNINGS_SAFE_PERCENTILE: float = 0.85     # percentile of moves the strike must clear
    EARNINGS_STRIKE_ROUND: float = 5.0         # round selected strikes to nearest $
    EARNINGS_MIN_STOCK_PRICE: float = 20.0     # avoid penny stocks
    EARNINGS_MIN_OPTIONS_VOLUME: int = 1000
    EARNINGS_MIN_OPEN_INTEREST: int = 5000
    EARNINGS_SPIKE_TARGET_PROFIT_PCT: float = 30.0  # IV-spike exit target before earnings
    EARNINGS_IV_SPIKE_FORCE_EXIT_DTE: int = 1   # force-exit IV spike at/under this DTE
    EARNINGS_CSV_PATH: str = "broker_client/earnings/data/upcoming_earnings.csv"

    # ── Drop / bounce detector (Phase 3, Module B) ────────────
    DROP_ALERT_THRESHOLD_PCT: float = 4.0      # alert on drops > 4%
    DROP_LOOKBACK_DAYS: int = 2                # check last 2 days
    DROP_FUNDAMENTAL_MIN_SIGNALS: int = 2      # 2+ fundamental signals = FUNDAMENTAL
    DROP_BOUNCE_MIN_SCORE: int = 55            # min bounce score to suggest a trade
    DROP_SECTOR_WIDE_PCT: float = -2.0         # sector ETF move = macro/sector issue
    DROP_PARTIAL_SECTOR_PCT: float = -0.5      # some sympathy selling
    # Bounce score component weights (must sum to 1.0).
    BOUNCE_WEIGHT_CAUSE: float = 0.30          # most important — what caused it?
    BOUNCE_WEIGHT_FUNDAMENTAL: float = 0.25    # fundamentals still intact?
    BOUNCE_WEIGHT_INSTITUTIONAL: float = 0.20  # smart money buying the dip?
    BOUNCE_WEIGHT_TECHNICAL: float = 0.15      # oversold?
    BOUNCE_WEIGHT_TIMING: float = 0.10         # good entry timing?
    BOUNCE_STRONG_BUY_SCORE: int = 75          # >= → STRONG_BUY / immediate
    # Bounce instrument selection (Bucket 3).
    BOUNCE_CALL_DELTA: float = 0.6             # slightly OTM call for sentiment bounce
    BOUNCE_LEAP_DELTA: float = 0.8             # deep ITM LEAP for macro-crash bounce
    BOUNCE_CALL_EXPIRY_WEEKS: int = 2          # sentiment bounce horizon
    BOUNCE_LEAP_EXPIRY_MONTHS: int = 18        # macro-crash LEAP horizon
    BOUNCE_CALL_PROFIT_TARGET_PCT: float = 50.0
    BOUNCE_LEAP_PROFIT_TARGET_PCT: float = 40.0
    BOUNCE_SPREAD_PROFIT_TARGET_PCT: float = 40.0

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
    _VALID_PROFILES: ClassVar[set[str]] = {
        "conservative",
        "moderate",
        "aggressive",
        "custom",
    }

    @property
    def cash_buffer_pct(self) -> int:
        """Portfolio % held as cash buffer (100 − sum of bucket allocations)."""
        return 100 - (
            self.BUCKET1_ALLOCATION_PCT
            + self.BUCKET2_ALLOCATION_PCT
            + self.BUCKET3_ALLOCATION_PCT
        )

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

        # Three-bucket framework (Phase 2): valid profile + allocations sum <= 100.
        if self.TRADING_PROFILE not in self._VALID_PROFILES:
            raise ConfigError(
                "Invalid TRADING_PROFILE",
                value=self.TRADING_PROFILE,
                allowed=sorted(self._VALID_PROFILES),
            )
        bucket_sum = (
            self.BUCKET1_ALLOCATION_PCT
            + self.BUCKET2_ALLOCATION_PCT
            + self.BUCKET3_ALLOCATION_PCT
        )
        if bucket_sum > 100:
            raise ConfigError(
                "Bucket allocations must sum to <= 100 (remainder = cash buffer)",
                actual_sum=bucket_sum,
            )

        # Bounce score component weights must sum to ~1.0 (Phase 3, Module B).
        bounce_weight_sum = (
            self.BOUNCE_WEIGHT_CAUSE
            + self.BOUNCE_WEIGHT_FUNDAMENTAL
            + self.BOUNCE_WEIGHT_INSTITUTIONAL
            + self.BOUNCE_WEIGHT_TECHNICAL
            + self.BOUNCE_WEIGHT_TIMING
        )
        if abs(bounce_weight_sum - 1.0) > 1e-6:
            raise ConfigError(
                "Bounce score weights must sum to 1.0", actual_sum=bounce_weight_sum
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
