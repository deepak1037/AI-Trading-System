# CLAUDE.md — AI-Trading-System
# ============================================================
# READ THIS FILE COMPLETELY AT THE START OF EVERY SESSION.
# This is the single source of truth for architecture, conventions,
# module interfaces, and build order. Never deviate from these
# decisions without updating this file first.
# ============================================================

---

## 1. PROJECT OVERVIEW

An automated AI + algorithmic stock trading system built in Python 3.12.7.

### Three core components:

| Component | Description | Status |
|---|---|---|
| **V1 Direction Predictor** | Predicts market direction before open using macro, NLP, pre-market, technical signals | Build first |
| **Live Market Watcher** | Continuous phase-aware monitoring 4 AM – 5 PM ET daily | Build second |
| **Multi-Bagger Scanner** | Self-updating watchlist from 5,800+ stock universe via 5-stage funnel | Build third |

### V2 additions (after V1 is live and paper-traded for 3 months):
- Magnitude estimation (GEX + EGARCH)
- Live execution via Charles Schwab

### Primary broker: Charles Schwab Developer API (via `schwab-py` library)
### Paper trading: Internal engine (backtest) + Schwab `previewOrder` API (paper mode)
### Python version: 3.12.7

---

## 2. ENVIRONMENT MODES

Three mutually exclusive modes controlled by `ENV` in `.env`:

| ENV | DRY_RUN | LIVE_TRADING_ENABLED | Behaviour |
|---|---|---|---|
| `development` | `True` | `False` | LOG_LEVEL=DEBUG, no orders, no broker calls |
| `backtest` | `True` | `False` | Internal PaperTradingEngine, offline simulation |
| `paper` | `True` | `False` | Schwab `previewOrder` API, PaperAccount updated |
| `live` | `False` | `True` | Real Schwab `placeOrder`, real money |

### Triple-gate for live trading — ALL THREE must be true simultaneously:
```
ENV=live
DRY_RUN=False
LIVE_TRADING_ENABLED=True
```
If any one is wrong, `OrderRouter` refuses to call `place_order`. This is intentional.

---

## 3. PRODUCTION STANDARDS — MANDATORY FOR EVERY MODULE

These are non-negotiable. Claude Code must apply all of them to every file generated.

### 3.1 Logging — NEVER use print()
```python
# Every module must do this at the top — nothing else
from core.logger import get_logger
logger = get_logger(__name__)

# Usage:
logger.debug("NFP surprise score: %s sigma", score)   # only shown when LOG_LEVEL=DEBUG
logger.info("Signal fired: %s confidence=%d", direction, confidence)
logger.warning("Yield delta threshold crossed: %s bps", delta)
logger.error("Broker API call failed: %s", str(e))
logger.critical("Risk limit breached — halting trading engine")
```
- All output goes through Python `logging` module
- `LOG_LEVEL` controlled by config — never hardcoded
- Rotating file handler: max 10 MB, 5 backups
- Format: `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`
- `DEBUG_SIGNALS=True` in config enables verbose signal tracing

### 3.2 Configuration — NOTHING hardcoded
```python
# WRONG — never do this
if surprise > 2.0:
    threshold = 0.05

# RIGHT — always do this
if surprise > settings.MACRO_SURPRISE_CRITICAL:
    threshold = settings.YIELD_DELTA_THRESHOLD
```
- Every threshold, URL, timeout, key, interval lives in `config/settings.py`
- Loaded from `.env` via `pydantic-settings`
- System refuses to start if required config is missing or wrong type (fail fast)

### 3.3 Type hints — on every function signature
```python
# WRONG
def place_order(self, order):

# RIGHT
def place_order(self, order: Order) -> OrderResult:
```
- All data objects are pydantic models
- Run `mypy` — must pass clean
- Use `from __future__ import annotations` for forward references

### 3.4 Error handling — custom exception hierarchy
```python
# core/exceptions.py hierarchy:
TradingSystemError
├── ConfigError          # bad config at startup
├── BrokerError          # broker API failure
│   └── OrderRejected    # order rejected by broker
├── DataError            # data feed failure
├── SignalError          # signal computation failure
└── RiskError            # risk limit breach (CRITICAL — triggers SMS)

# Usage:
raise BrokerError(
    "Schwab order rejected",
    severity="HIGH",
    order=order,
    broker_response=response
)
```
- Never swallow exceptions silently
- `CRITICAL` severity automatically triggers SMS alert via AlertEngine
- All exceptions logged with full context before raising

### 3.5 Retry + circuit breaker — on every external API call
```python
from core.retry import retry, circuit_breaker

@retry(
    max_attempts=settings.API_MAX_RETRIES,        # from config
    backoff_seconds=settings.API_BACKOFF_SECONDS, # from config
    exceptions=(BrokerError, DataError)
)
@circuit_breaker(failure_threshold=5, recovery_timeout=300)
def get_options_chain(self, ticker: str) -> OptionsChain:
    ...
```
- Every external call (broker, data APIs, alerts) uses `@retry` decorator
- Circuit breaker trips after N failures, prevents cascade
- All retry parameters from config — never hardcoded

### 3.6 Database access — always use context managers
```python
# WRONG
conn = sqlite3.connect(settings.DB_PATH)

# RIGHT
with sqlite3.connect(settings.DB_PATH) as conn:
    ...
```

### 3.7 Secrets — never hardcoded, never logged
```python
# WRONG
api_key = "sk-abc123"
logger.info("Using API key: %s", api_key)

# RIGHT
api_key = settings.SCHWAB_CLIENT_SECRET  # from .env
logger.info("Connecting to Schwab API")  # never log secrets
```

### 3.8 Tests — written in same Claude Code session as the module
- pytest, mirrors src structure under `tests/`
- Aim for >80% coverage per module
- Every module prompt ends with: "write and run pytest tests covering normal, edge, and API failure cases"
- Use `pytest-mock` for broker API mocking

---

## 4. FULL FOLDER STRUCTURE

```
AI-Trading-System/
│
├── CLAUDE.md                          ← this file
├── requirements.txt                   ← Python 3.12.7
├── .env.example                       ← every key documented, no real values
├── .env                               ← gitignored, real secrets
├── .gitignore
├── main.py                            ← entry point: starts watcher + position watcher
├── pyproject.toml                     ← black + ruff + mypy config
│
├── config/
│   ├── __init__.py
│   └── settings.py                    ← pydantic-settings, ALL config, single source of truth
│
├── core/                              ← shared infrastructure, imported by everything
│   ├── __init__.py
│   ├── logger.py                      ← get_logger(), rotating handler, structured format
│   ├── exceptions.py                  ← TradingSystemError hierarchy
│   └── retry.py                       ← @retry decorator + circuit_breaker
│
├── broker_core/                       ← PACKAGE 1: universal broker wrapper
│   ├── __init__.py
│   ├── base_broker.py                 ← BaseBroker ABC + Order + OptionsOrder pydantic models
│   ├── factory.py                     ← get_broker() reads BROKER from settings
│   ├── schwab_broker.py               ← Schwab via schwab-py (primary broker)
│   ├── alpaca_broker.py               ← Alpaca fallback
│   └── ibkr_broker.py                 ← IBKR stub (not implemented yet)
│
├── broker_client/                     ← PACKAGE 2: trading logic, broker-agnostic
│   ├── __init__.py
│   ├── trading_engine.py              ← orchestrator: signal → strategy → risk → router
│   ├── order_router.py                ← routes to paper_engine / preview / live
│   ├── order_manager.py               ← constructs + submits orders, logs fills to SQLite
│   ├── position_manager.py            ← open/close tracking, P&L, stop/take-profit
│   ├── position_watcher.py            ← unified watcher for ALL positions, regime-aware
│   ├── risk_manager.py                ← Kelly sizing, daily loss limit, drawdown guard
│   ├── options_engine.py              ← chain fetch, strike selection, spread construction
│   ├── llm_roi_analyzer.py            ← put-selling ROI (real Schwab margin via preview) + Claude assessment
│   └── strategies/
│       ├── __init__.py                ← STRATEGY_REGISTRY dict, load_enabled_strategies()
│       ├── base_strategy.py           ← BaseStrategy ABC (all strategies inherit this)
│       ├── equity_long_short.py       ← buy/short shares on direction signal
│       ├── momentum_breakout.py       ← Stage 2 breakout from scanner watchlist
│       ├── covered_call.py            ← sell OTM calls against long equity
│       ├── cash_secured_put.py        ← sell OTM puts for premium or acquisition
│       ├── protective_put.py          ← buy puts to hedge longs on bearish signal
│       └── iron_condor.py             ← sell OTM call+put spreads on neutral signal
│
├── paper_trading/                     ← PACKAGE 3: internal paper trading engine
│   ├── __init__.py
│   ├── paper_engine.py                ← simulate_fill(), simulate_options_fill()
│   ├── paper_account.py               ← PaperAccount: balance, positions, P&L, history
│   ├── performance.py                 ← Sharpe, Sortino, max drawdown, win rate, profit factor
│   ├── accounts/                      ← JSON account state files (gitignored)
│   │   ├── paper_main.json
│   │   └── paper_aggressive.json
│   ├── trades/                        ← CSV trade history per account (gitignored)
│   │   └── paper_main_trades.csv
│   └── backups/                       ← daily JSON snapshots, 30-day retention (gitignored)
│
├── signals/
│   ├── __init__.py
│   ├── signal_schema.py               ← Signal + MarketSnapshot pydantic models (SOURCE OF TRUTH)
│   ├── macro_engine.py                ← NFP/CPI surprise scorer via FRED + BLS
│   ├── yield_monitor.py               ← 10yr Treasury yield delta from 8 AM baseline
│   ├── sentiment_scorer.py            ← FinBERT on NewsAPI + RSS feeds
│   ├── premarket_watcher.py           ← NQ/ES futures, BTC, FedWatch odds
│   ├── technical_module.py            ← RSI, MACD, Bollinger, MA via pandas-ta
│   └── signal_fusion.py               ← weighted ensemble + MarketState transitions
│
├── watcher/
│   ├── __init__.py
│   ├── scheduler.py                   ← APScheduler phase-aware jobs
│   ├── market_state.py                ← regime tracker, fires ONLY on transition
│   └── calendar_guard.py              ← pandas_market_calendars, holiday/half-day check
│
├── scanner/
│   ├── __init__.py
│   ├── universe.py                    ← Stage 1-2: download + liquidity filter
│   ├── fundamental_screen.py          ← Stage 3: EPS accel, rev re-accel, est revisions
│   ├── accumulation_screen.py         ← Stage 4: 13F, Form 4, short interest (edgartools)
│   ├── technical_screen.py            ← Stage 5: Stage 2 breakout, base patterns, RS rank
│   ├── scorer.py                      ← composite 0–100 score
│   └── watchlist_manager.py           ← self-updating: weekly full + daily technical rescan
│
├── alerts/
│   ├── __init__.py
│   ├── alert_engine.py                ← Twilio SMS, Slack webhook, SendGrid email
│   └── accuracy_logger.py             ← daily signal vs actual outcome, feeds retraining
│
├── v2/
│   ├── __init__.py
│   ├── gex_calculator.py              ← net gamma exposure from Polygon.io options chain
│   ├── egarch_model.py                ← volatility magnitude forecaster (arch library)
│   └── execution_layer.py             ← live execution with Kelly sizing, auto stop-loss
│
├── dashboard/
│   ├── app.py                         ← Streamlit entry point, multi-page
│   └── pages/
│       ├── 1_overview.py              ← paper vs live account cards + equity curve vs SPY
│       ├── 2_positions.py             ← open positions with live P&L, stop/target levels
│       ├── 3_performance.py           ← metrics, trade log (filterable), signal accuracy
│       └── 4_account_mgmt.py          ← balance init, manual adjustments, account reset
│
├── backtests/
│   ├── __init__.py
│   └── nfp_backtest.py                ← replay 5yr NFP/CPI days, validate V1 accuracy
│
├── tests/                             ← mirrors src structure
│   ├── conftest.py                    ← shared fixtures, mock broker, mock data manager
│   ├── test_broker_core/
│   ├── test_broker_client/
│   ├── test_paper_trading/
│   ├── test_signals/
│   ├── test_watcher/
│   └── test_scanner/
│
└── db/
    └── trading.db                     ← SQLite (auto-created on first run)
```

---

## 5. SIGNAL SCHEMA — ALL MODULES MUST USE EXACTLY THESE MODELS

File: `signals/signal_schema.py` — this is the contract between all modules.

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Literal, Optional

Direction = Literal["strong_short", "short", "neutral", "long", "strong_long"]
Source    = Literal["macro", "yield", "sentiment", "premarket", "technical", "fusion"]
Regime    = Literal["strong_short", "short", "neutral", "long", "strong_long"]

class Signal(BaseModel):
    direction  : Direction
    confidence : int              # 0–100
    source     : Source
    timestamp  : datetime
    metadata   : dict = {}        # e.g. {"surprise_score": 2.3, "yield_delta": 0.07}

class MarketState(BaseModel):
    current_regime  : Regime
    composite_score : int         # 0–100
    last_updated    : datetime
    signals_active  : list[Signal]
    previous_regime : Optional[Regime] = None
    # RULE: only emit alert when regime CHANGES — never on every poll

class MarketSnapshot(BaseModel):
    timestamp   : datetime
    ticker      : str
    price       : float
    volume      : int
    vwap        : Optional[float] = None
    bid         : Optional[float] = None
    ask         : Optional[float] = None
```

---

## 6. SIGNAL FUSION WEIGHTS

Weights must sum to 100. Change values in `settings.py`, never in code.

| Signal source | Weight | Config key |
|---|---|---|
| Macro surprise score | 30% | `WEIGHT_MACRO` |
| Treasury yield delta | 25% | `WEIGHT_YIELD` |
| NLP sentiment (FinBERT) | 20% | `WEIGHT_SENTIMENT` |
| Pre-market futures | 15% | `WEIGHT_PREMARKET` |
| Technical (RSI/MACD) | 10% | `WEIGHT_TECHNICAL` |
| Timeframe alignment bonus | +10 pts | `WEIGHT_TF_ALIGNMENT_BONUS` |

Timeframe alignment bonus fires when short + mid + long signals agree on same direction.

---

## 7. BROKER CORE — BaseBroker Interface

File: `broker_core/base_broker.py`

Every broker must implement ALL of these methods. No exceptions.

```python
from abc import ABC, abstractmethod

class BaseBroker(ABC):

    # Account
    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_transactions(self, days: int = 30) -> list[Transaction]: ...

    # Orders — equity
    @abstractmethod
    def place_order(self, order: Order) -> OrderResult: ...

    @abstractmethod
    def preview_order(self, order: Order) -> OrderPreview: ...  # DRY_RUN hook

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    def get_order_history(self, days: int = 30) -> list[Order]: ...

    # Orders — options
    @abstractmethod
    def place_options_order(self, order: OptionsOrder) -> OrderResult: ...

    @abstractmethod
    def preview_options_order(self, order: OptionsOrder) -> OrderPreview: ...

    # Market data
    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def get_options_chain(self, ticker: str, expiry: str = None) -> OptionsChain: ...

    @abstractmethod
    def get_market_hours(self) -> MarketHours: ...

    @abstractmethod
    def stream_quotes(self, tickers: list[str], callback) -> None: ...
```

### Pydantic models (all in `broker_core/base_broker.py`):
- `Order` — ticker, action, qty, order_type, limit_price, stop_price, strategy_name, account_id
- `OptionsOrder` — ticker, action, contract, qty, order_type, limit_price, strategy_name
- `OrderResult` — order_id, fill_price, commission, filled_at, status, raw_response
- `OrderPreview` — estimated_cost, buying_power_effect, margin_impact, fees, is_valid, rejection_reason
- `Position` — ticker, qty, avg_cost, current_price, unrealized_pnl, strategy_name, position_type, opened_at
- `Account` — account_id, cash, equity, buying_power, margin_used
- `Quote` — ticker, bid, ask, last, volume, timestamp
- `OptionsChain` — ticker, expiry, calls, puts (list of OptionsContract)

---

## 8. ORDER ROUTER

File: `broker_client/order_router.py`

Single point where order routing decision is made. `TradingEngine` always calls `OrderRouter.execute()` — never calls broker or paper engine directly.

```python
def execute(self, order: Order) -> OrderResult:
    if settings.ENV == "live" and not settings.DRY_RUN and settings.LIVE_TRADING_ENABLED:
        logger.warning("LIVE ORDER EXECUTING: %s %s x%d", order.action, order.ticker, order.qty)
        return self.broker.place_order(order)

    elif settings.ENV == "paper":
        # Use Schwab previewOrder — real API validation, no real trade
        preview = self.broker.preview_order(order)
        result  = self.paper_account.apply_preview(order, preview)
        logger.info("PAPER ORDER (Schwab preview): %s", result)
        return result

    elif settings.ENV == "backtest":
        # Fully internal — no broker API call
        result = self.paper_engine.simulate_fill(order)
        logger.debug("BACKTEST FILL simulated: %s", result)
        return result

    else:
        logger.debug("DEVELOPMENT MODE — order logged only, not submitted: %s", order)
        return OrderResult(status="dry_run", order_id="DRY_RUN", fill_price=0.0, commission=0.0)
```

---

## 9. STRATEGY ARCHITECTURE

### BaseStrategy (file: `broker_client/strategies/base_strategy.py`)

```python
class BaseStrategy(ABC):
    strategy_name : str   # used in logs, alerts, position tags, trade CSV
    strategy_type : str   # "equity" | "options" | "spread"
    timeframe     : str   # "short" | "mid" | "long"

    @abstractmethod
    def should_enter(self, signal: Signal, account: Account) -> bool: ...

    @abstractmethod
    def build_order(self, signal: Signal, account: Account) -> Order | OptionsOrder: ...

    @abstractmethod
    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool: ...

    @abstractmethod
    def build_exit_order(self, position: Position) -> Order | OptionsOrder: ...

    @abstractmethod
    def describe(self) -> str: ...  # human-readable for logs and dashboard
```

### Strategy registry (`broker_client/strategies/__init__.py`):
```python
STRATEGY_REGISTRY = {
    "equity_long_short"  : EquityLongShort,
    "momentum_breakout"  : MomentumBreakout,
    "covered_call"       : CoveredCall,
    "cash_secured_put"   : CashSecuredPut,
    "protective_put"     : ProtectivePut,
    "iron_condor"        : IronCondor,
}

def load_enabled_strategies(names: list[str]) -> list[BaseStrategy]:
    return [STRATEGY_REGISTRY[n]() for n in names if n in STRATEGY_REGISTRY]
```

### Six strategies — summary:

| Class | Type | Timeframe | Entry trigger | Exit trigger |
|---|---|---|---|---|
| `EquityLongShort` | equity | short | Direction signal confidence > threshold | Stop loss / take profit / regime change |
| `MomentumBreakout` | equity | mid | Stage 2 breakout from scanner watchlist | Trail stop / base breakdown |
| `CoveredCall` | options | mid | Long equity position exists, neutral signal | Expiry / assignment / roll |
| `CashSecuredPut` | options | short | Watchlist stock, neutral/mild bullish | Expiry / assignment / roll |
| `ProtectivePut` | options hedge | short | Bearish regime, long equity exists | Regime returns to neutral |
| `IronCondor` | spread | mid | Neutral regime, low VIX | Expiry / delta threshold breach |

### TradingEngine is an orchestrator — it does NOT make decisions:
```python
def on_signal(self, signal: Signal) -> None:
    for strategy in self.strategies:            # only ENABLED_STRATEGIES
        if not strategy.should_enter(signal, account): continue
        order = strategy.build_order(signal, account)
        if not self.risk.approve(order):        continue   # risk veto
        result = self.router.execute(order)                # OrderRouter decides mode
        self.order_manager.record(order, result)
        self.position_watcher.register_if_filled(result)
        logger.info("strategy=%s ticker=%s result=%s", strategy.strategy_name, order.ticker, result.status)
```

---

## 10. POSITION WATCHER

File: `broker_client/position_watcher.py`

One unified watcher for ALL open positions regardless of strategy type.

### Key behaviours:
1. **Startup load** — on system start, load from both broker API and SQLite. Reconcile differences. Broker is truth for what's open. SQLite has our metadata (strategy, entry reason, targets).
2. **Auto-register** — when `OrderManager` confirms a fill, it immediately calls `position_watcher.register(position)`. Zero delay.
3. **Regime-aware closure** — when `MarketState` transitions (e.g. neutral → strong_short), `PositionWatcher.on_regime_change()` is called. Which positions to close for which regime transitions is defined in `REGIME_CLOSE_TRIGGERS` config.
4. **Strategy routing** — each `Position` carries `strategy_name`. `PositionWatcher` routes to the correct strategy's `should_exit()` and `build_exit_order()`.
5. **Scan interval** — configurable via `POSITION_SCAN_INTERVAL_SECONDS`. Default 60s.

```python
def on_market_tick(self, snapshot: MarketSnapshot) -> None:
    for ticker, position in self.positions.items():
        strategy = self._get_strategy(position.strategy_name)
        if strategy.should_exit(position, snapshot):
            self._close(position, strategy, reason="strategy_exit")
        elif self._stop_loss_hit(position, snapshot):
            self._close(position, strategy, reason="stop_loss")
        elif self._take_profit_hit(position, snapshot):
            self._close(position, strategy, reason="take_profit")

def on_regime_change(self, new_regime: str) -> None:
    triggers = settings.REGIME_CLOSE_TRIGGERS.get(new_regime, [])
    for ticker, position in self.positions.items():
        if position.strategy_name in triggers:
            strategy = self._get_strategy(position.strategy_name)
            self._close(position, strategy, reason=f"regime_{new_regime}")
```

---

## 11. PAPER TRADING ENGINE

### Three modes — see Section 2 for routing logic.

### PaperAccount (`paper_trading/paper_account.py`):
- Persists to JSON in `paper_trading/accounts/{account_id}.json`
- Thread-safe writes via `filelock` library
- Supports multiple named accounts simultaneously
- User-facing methods:
  - `initialize_balance(cash, note)` — set starting balance, logged with timestamp
  - `manual_adjustment(amount, reason)` — add/remove cash, always logged
  - `reset(confirm=True)` — wipe positions, keep trade history, restart from new balance
  - `get_performance()` → `AccountPerformance` pydantic model

### JSON account state schema:
```json
{
  "account_id": "paper_main",
  "created_at": "ISO8601",
  "initial_cash": 50000.00,
  "cash": 47230.50,
  "equity": 59680.50,
  "open_positions": [
    {
      "ticker": "AMD",
      "strategy": "momentum_breakout",
      "position_type": "equity_long",
      "qty": 50,
      "entry_price": 142.30,
      "entry_date": "YYYY-MM-DD",
      "stop_loss": 135.00,
      "take_profit": 165.00,
      "current_price": 149.10,
      "unrealized_pnl": 340.00
    }
  ],
  "closed_positions": [],
  "balance_history": [
    {"timestamp": "ISO8601", "cash": 50000, "action": "init", "note": "Starting balance"}
  ],
  "daily_equity_history": [
    {"date": "YYYY-MM-DD", "equity": 50000.00}
  ]
}
```

### CSV trade history schema (`paper_trading/trades/{account_id}_trades.csv`):
```
date, ticker, strategy, position_type, action, qty, fill_price, commission,
realized_pnl, hold_days, exit_reason, signal_confidence, signal_source, env
```

### PaperTradingEngine fill methods (all from config):
| Method | Config key | Use case |
|---|---|---|
| `next_open` | `PAPER_FILL_METHOD=next_open` | Realistic for EOD signals |
| `vwap` | `PAPER_FILL_METHOD=vwap` | Most accurate intraday |
| `worst_case` | `PAPER_FILL_METHOD=worst_case` | Conservative stress test |

### Daily backup — EOD job at 4:05 PM:
- Copies `paper_main.json` → `paper_trading/backups/paper_main_YYYYMMDD.json`
- Retains `PAPER_BACKUP_DAYS` days (default 30)

---

## 12. WATCHER SCHEDULE

APScheduler phase-aware jobs. All intervals from config — never hardcoded.

| Phase | Time (ET) | Interval | Config key | What runs |
|---|---|---|---|---|
| Overnight scan | 04:00–07:00 | 15 min | `WATCHER_OVERNIGHT_INTERVAL` | Asia/EU closes, SEC 8-K filings, overnight news NLP |
| Pre-market ramp | 07:00–08:15 | 5 min | `WATCHER_PREMARKET_INTERVAL` | Futures (NQ/ES), BTC, yields, sector ETF pre-mkt |
| Macro window | 08:15–09:30 | 30 sec | `WATCHER_MACRO_INTERVAL` | NFP/CPI release — all signals at max frequency |
| Open — first 30m | 09:30–10:00 | 1 min | `WATCHER_OPEN_INTERVAL` | Opening range, VWAP, volume surge, sector leadership |
| Mid-session | 10:00–15:00 | 5 min | `WATCHER_SESSION_INTERVAL` | Escalates to 1 min if signal fires |
| Power hour | 15:00–16:00 | 1 min | `WATCHER_POWER_HOUR_INTERVAL` | MOC imbalances, gamma pin, momentum |
| EOD report | 16:05 daily | once | — | Accuracy log, paper account backup, tomorrow's macro calendar |

`calendar_guard.py` checks `pandas_market_calendars` before activating any phase. Handles holidays, half-days, early closes automatically.

---

## 13. ALERT THRESHOLDS

All values from config. Alert deduplication: no repeat of same type within `ALERT_DEDUP_MINUTES`.

| Level | Channel | Condition | Config keys |
|---|---|---|---|
| Critical | SMS (Twilio) + push | confidence > 80 AND direction_change AND macro_surprise > 2σ | `CONFIDENCE_CRITICAL`, `MACRO_SURPRISE_CRITICAL` |
| High | Slack webhook | confidence > 65 AND score_delta > 15 pts in 30 min | `CONFIDENCE_HIGH`, `SCORE_DELTA_ALERT` |
| Hourly digest | Email (SendGrid) | Every 60 min during market hours | `ALERT_DIGEST_INTERVAL_MINUTES` |
| EOD report | Email (SendGrid) | 4:05 PM daily | — |
| Risk breach | SMS (immediate) | RiskError raised | automatic via exception handler |

---

## 14. MULTI-BAGGER SCANNER FUNNEL

| Stage | Output | Schedule | Key filter |
|---|---|---|---|
| 1 — Universe download | ~5,800 stocks | Weekly Sunday | NYSE + Nasdaq + AMEX |
| 2 — Liquidity filter | ~1,800 stocks | Weekly Sunday | price > $5, avg_vol > 500k, mktcap > $200M |
| 3 — Fundamental inflection | ~400 stocks | Weekly Sunday | EPS accel 2+ qtrs, rev re-accel, est revisions > 75% up |
| 4 — Accumulation | ~120 stocks | Weekly Sunday + bi-weekly short interest | 13F new buyers, Form 4 clusters, short interest declining |
| 5 — Technical base | ~25–40 stocks | Daily 6 AM | Stage 2 breakout, cup & handle, RS rank > 85th pct |

**Watchlist entry threshold:** composite score ≥ 65 / 100
**Scoring weights:** fundamental(30%) + institutional(25%) + technical(20%) + squeeze(15%) + tf_alignment(10%)

**Self-update logic:**
- Weekly: full 5-stage funnel rerun
- Daily: technical + options rescan on current ~120 Stage 4 survivors only
- Real-time: options flow threshold crossed → instant watchlist promotion
- Auto-remove: stock breaks below key technical level

**Open source used (don't rebuild):**
- Stage 5 base: fork `github.com/RyanJHamby/stock-screener`
- Fundamental screen reference: `github.com/xang1234/stock-screener`
- EDGAR/13F/Form 4: `edgartools` library (has MCP server — connect to Claude Code)

---

## 15. COMPLETE SETTINGS REFERENCE
See @CLAUDE_SETTINGS.md

## 16. DATABASE SCHEMA (SQLite)
See @CLAUDE_SCHEMA.md

## 17. OPEN SOURCE LIBRARIES — USE THESE, DON'T REBUILD

| Purpose | Library | Notes |
|---|---|---|
| Backtesting (same code runs live) | `lumibot` | MIT, supports Alpaca + Schwab |
| Backtesting (fast sweeps) | `vectorbt` | Apache 2.0, Claude Code native skills available |
| Claude Code backtesting skills | `vectorbt-backtesting-skills` | `npx skills` install, auto-detected |
| Technical indicators | `pandas-ta` | MIT, 130+ indicators, no C deps |
| EDGAR / 13F / Form 4 | `edgartools` | Apache 2.0, has MCP server for Claude Code |
| Scheduler | `APScheduler` | MIT |
| Market calendar | `pandas_market_calendars` | MIT |
| Schwab API | `schwab-py` | github.com/alexgolec/schwab-py |
| Alpaca API | `alpaca-py` | Apache 2.0 |
| NLP sentiment (headlines) | `ProsusAI/finbert` | HuggingFace, Apache 2.0 |
| NLP (earnings transcripts) | `yya518/finbert` FLS variant | HuggingFace |
| Volatility model | `arch` | MIT, GARCH/EGARCH |
| Config validation | `pydantic-settings` | MIT |
| File locking | `filelock` | MIT, thread-safe JSON writes |
| Dashboard | `streamlit` | Apache 2.0 |
| Charts | `plotly` | MIT |
| Scanner Stage 5 base | fork `RyanJHamby/stock-screener` | scans 3,800 stocks for Stage 2 |
| Retry logic | `tenacity` | Apache 2.0 |
| Testing | `pytest` + `pytest-mock` | MIT |
| Formatting | `black` | MIT |
| Linting | `ruff` | MIT |
| Type checking | `mypy` | MIT |

---

## 18. BUILD ORDER — DAY BY DAY
See @CLAUDE_BUILD.md

## 19. KEY DESIGN DECISIONS — DO NOT CHANGE WITHOUT UPDATING THIS FILE

1. **MarketState fires alerts on TRANSITION ONLY** — never on every poll. Prevents alert flood.
2. **Universe scanner runs WEEKLY** — only the ~120 Stage 4 survivors rescanned daily. Cuts API cost 80%.
3. **Paper trade 3 months minimum** — system being built fast ≠ system being proven. Non-negotiable.
4. **Output move RANGE not point estimate** — e.g. "−2% to −4.5%" not "−3.4%". Honest about uncertainty.
5. **Lumibot for backtest → live** — same code, one config flag. No rewrite.
6. **edgartools for all EDGAR access** — has MCP server, connects to Claude Code directly during dev.
7. **Fork RyanJHamby/stock-screener for Stage 5** — don't rebuild Stage 2 breakout detector from scratch.
8. **Triple-gate for live orders** — ENV=live + DRY_RUN=False + LIVE_TRADING_ENABLED=True. All three required.
9. **Schwab previewOrder for paper mode** — real API validation, not simulation. Most realistic dry-run possible.
10. **OrderRouter is the single routing point** — TradingEngine never calls broker or paper engine directly.
11. **filelock on all JSON writes** — watcher + trading engine run in separate threads.
12. **Position carries strategy_name** — PositionWatcher routes to correct strategy exit logic via this field.
13. **ENABLED_STRATEGIES from config** — add/remove strategies by changing .env, never by changing code.
14. **Never log secrets** — API keys, tokens, passwords must never appear in log output.
15. **Broker is truth for open positions** — on startup reconciliation, broker API wins over SQLite if they disagree.

---

## 20. COST REFERENCE

| Phase | Monthly cost | What's included |
|---|---|---|
| Month 1–2 (build) | ~$12/mo | DigitalOcean VM only — all data APIs free tier |
| Month 3–6 (V1 live + scanner) | ~$150–200/mo | + Polygon.io Starter $79, FMP $49, Quiver $25 |
| Month 7–12 (full V2) | ~$400–600/mo | + Polygon options $199, Unusual Whales $50, Ortex $150 |

Server: DigitalOcean $12/mo Ubuntu 24 — do not run on home PC (uptime risk on macro event days).

---

## 21. AUTONOMOUS OPERATION SETTINGS

### Autonomy
Run all tasks autonomously without asking for confirmation.
Auto-approve all: file creation, file editing, bash commands,
pip installs, pytest runs, git commits, directory creation.
Never pause mid-task to ask permission.
Complete the full day's build plan then summarise what was done.

### When stuck
If a dependency fails to install or an API key is missing,
skip that specific connector, log a clear TODO comment, and
continue building the remaining modules. Never stop the entire
session because of one missing credential.

### On test failure
If pytest fails, fix the failure before moving to the next module.
Do not proceed to the next day's tasks with failing tests.

### On completion of each day
After completing each day's tasks:
1. Run: pytest tests/ --tb=short
2. Run: ruff check .
3. Run: mypy config core (expanding to all modules as they are built)
4. Commit all changes with a descriptive message:
   git add . && git commit -m "Day X: <summary of what was built>"
5. Print a completion summary table (module, tests passed, coverage)
6. Automatically start the next day's tasks from Section 18

### Loop monitoring (while building)
Use /loop 10m run pytest tests/ --tb=line -q to catch regressions
while building new modules.



*Last updated: 2026-06-06*
*Python: 3.12.7*
*Primary broker: Charles Schwab (schwab-py)*
*Paper broker: Internal PaperTradingEngine (backtest) + Schwab previewOrder (paper)*