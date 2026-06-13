# AI-Trading-System — Verification & Audit Skill

## Purpose
Run this skill to audit the entire codebase against the agreed design.
Identify what is built, what is missing, what is broken, and what needs fixing.

## How to run
Tell Claude Code:
```
Read and execute @CLAUDE_VERIFY.md
Audit the entire codebase and produce a full report.
```

---

## SECTION 1 — File Structure Audit

Verify ALL expected files exist. For each file check:
- File exists ✅ / Missing ❌
- File is non-empty (not a stub)
- No syntax errors (import the module)

### Core Infrastructure
```
config/settings.py
core/logger.py
core/exceptions.py
core/retry.py
data/db.py
data/rate_limiter.py
data/data_manager.py
data/yfinance_connector.py
data/alpaca_connector.py
data/fred_connector.py
data/edgar_connector.py
```

### Signal System
```
signals/signal_schema.py
signals/macro_engine.py
signals/yield_monitor.py
signals/sentiment_scorer.py
signals/premarket_watcher.py
signals/technical_module.py
signals/signal_fusion.py
signals/bls_connector.py
```

### Watcher
```
watcher/scheduler.py
watcher/market_state.py
watcher/calendar_guard.py
```

### Broker Core
```
broker_core/base_broker.py
broker_core/schwab_broker.py
broker_core/alpaca_broker.py
broker_core/factory.py
broker_core/tokens/schwab_token.json (gitignored — check folder exists)
```

### Broker Client
```
broker_client/order_router.py
broker_client/trading_engine.py
broker_client/risk_manager.py
broker_client/order_manager.py
broker_client/position_manager.py
broker_client/position_watcher.py
broker_client/options_engine.py
broker_client/llm_roi_analyzer.py
broker_client/margin_calculator.py
broker_client/strategies/base_strategy.py
broker_client/strategies/equity_long_short.py
broker_client/strategies/momentum_breakout.py
broker_client/strategies/covered_call.py
broker_client/strategies/cash_secured_put.py
broker_client/strategies/protective_put.py
broker_client/strategies/iron_condor.py
```

### Paper Trading
```
paper_trading/paper_account.py
paper_trading/paper_engine.py
paper_trading/performance.py
paper_trading/accounts/paper_main.json
```

### Scanner
```
scanner/universe.py
scanner/fundamental_screen.py
scanner/accumulation_screen.py
scanner/technical_screen.py
scanner/scorer.py
scanner/watchlist_manager.py
```

### Alerts
```
alerts/alert_engine.py
alerts/accuracy_logger.py
```

### Dashboard
```
dashboard/app.py
dashboard/pages/1_overview.py
dashboard/pages/2_positions.py
dashboard/pages/3_performance.py
dashboard/pages/4_account_mgmt.py
dashboard/pages/6_options_analyzer.py
```

### Backtests
```
backtests/nfp_backtest.py
```

### V2
```
v2/gex_calculator.py
v2/egarch_model.py
v2/execution_layer.py
```

### Infrastructure
```
main.py
health.py
Makefile
requirements.txt
pyproject.toml
.env.example
.gitignore
trading-system.service
docs/runbook.md
scripts/healthcheck.py
```

---

## SECTION 2 — Settings Audit

Check config/settings.py has ALL these fields defined:

### System
```python
ENV: str
LOG_LEVEL: str
LOG_FILE: str
DEBUG_SIGNALS: bool
DRY_RUN: bool
LIVE_TRADING_ENABLED: bool
```

### Broker
```python
BROKER: str
SCHWAB_CLIENT_ID: str
SCHWAB_CLIENT_SECRET: str
SCHWAB_REDIRECT_URI: str
SCHWAB_ACCOUNT_NUMBER: str
SCHWAB_TOKEN_FILE: str
ALPACA_API_KEY: str
ALPACA_SECRET_KEY: str
ALPACA_PAPER: bool
```

### Signal Weights (must sum to 100)
```python
WEIGHT_MACRO: int        # 30
WEIGHT_YIELD: int        # 25
WEIGHT_SENTIMENT: int    # 20
WEIGHT_PREMARKET: int    # 15
WEIGHT_TECHNICAL: int    # 10
```

### Scanner Weights (must sum to 100)
```python
SCANNER_WEIGHT_FUNDAMENTAL: int    # 35
SCANNER_WEIGHT_INSTITUTIONAL: int  # 25
SCANNER_WEIGHT_TECHNICAL: int      # 30
SCANNER_WEIGHT_SQUEEZE: int        # 5
SCANNER_WEIGHT_TF_ALIGNMENT: int   # 5
```

### Thresholds
```python
MACRO_SURPRISE_CRITICAL: float     # 2.0
MACRO_SURPRISE_MODERATE: float     # 1.0
YIELD_DELTA_THRESHOLD: float       # 0.05
SENTIMENT_THRESHOLD: float         # 0.3
CONFIDENCE_CRITICAL: int           # 80
CONFIDENCE_HIGH: int               # 65
```

### APIs
```python
FRED_API_KEY: str
NEWS_API_KEY: str
BLS_API_KEY: str
FMP_API_KEY: str
ANTHROPIC_API_KEY: str
LLM_MODEL: str
```

### Moomoo
```python
MOOMOO_HOST: str           # 127.0.0.1
MOOMOO_PORT: int           # 11111
MOOMOO_ENABLED: bool       # True
MOOMOO_PACE_SECONDS: float # 1.1
```

### Alerts
```python
DISCORD_WEBHOOK_ALERTS: str
DISCORD_WEBHOOK_SIGNALS: str
DISCORD_WEBHOOK_OPPORTUNITIES: str
DISCORD_ENABLED: bool
TWILIO_ACCOUNT_SID: str
TWILIO_AUTH_TOKEN: str
TWILIO_FROM_NUMBER: str
TWILIO_TO_NUMBER: str
SLACK_WEBHOOK_URL: str
SENDGRID_API_KEY: str
ALERT_DEDUP_MINUTES: int
```

### Commission
```python
BACKTEST_OPTIONS_COMMISSION_PER_CONTRACT: float  # 0.50
BACKTEST_EQUITY_COMMISSION_PER_SHARE: float      # 0.00
# NOTE: paper/live fees come from Schwab previewOrder API
# NOT from settings
```

### CPI/Macro
```python
CPI_CONSENSUS_YOY: float      # 4.2
CORE_CPI_CONSENSUS_YOY: float # 2.9
CPI_RELEASE_LABEL: str        # 2026-05
```

### Geopolitical / Presidential
```python
GEOPOLITICAL_KEYWORDS: list
PRESIDENTIAL_FEED_URL: str    # trumpstruth.org/feed
PRESIDENTIAL_ALERT_MINUTES: int  # 15 (lookback window)
```

### Earnings (future use)
```python
EARNINGS_TARGET_CAPITAL: float
EARNINGS_DAYS_AHEAD: int
EARNINGS_IV_RANK_SELL_THRESHOLD: int
EARNINGS_IV_RANK_BUY_THRESHOLD: int
EARNINGS_MAX_BREACH_RATE: float
EARNINGS_ROLL_DAYS_BEFORE: int
```

---

## SECTION 3 — Functional Audit

Run each check and report PASS/FAIL:

### 3.1 Database
```python
# Check all 11 tables exist
from data.db import get_connection, init_db
init_db()
with get_connection() as conn:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    expected = ['signals','market_state_log','alerts_log',
                'accuracy_log','trades','positions','watchlist',
                'scanner_runs','ohlcv_cache','fundamentals_cache',
                'paper_accounts']  # adjust to actual
    for t in expected:
        status = '✅' if t in [r[0] for r in tables] else '❌ MISSING'
        print(f"{t}: {status}")
```

### 3.2 Schwab Broker
```python
# Auth, quote, options chain
from broker_core.factory import get_broker
broker = get_broker()
account = broker.get_account()
assert account.cash > 0, "No cash balance"
quote = broker.get_quote('AAPL')
assert quote.last > 0, "No quote"
chain = broker.get_options_chain('AAPL')
assert len(chain.calls) > 0, "No options chain"
print(f"✅ Schwab: cash=${account.cash:,.0f} AAPL=${quote.last}")
```

### 3.3 Signal Pipeline
```python
# Each signal module initialises and scores
from signals.macro_engine import MacroEngine
from signals.yield_monitor import YieldMonitor
from signals.sentiment_scorer import SentimentScorer
from signals.technical_module import TechnicalModule
from signals.signal_fusion import SignalFusion
import yfinance as yf

me = MacroEngine()
ym = YieldMonitor()
ss = SentimentScorer()
tm = TechnicalModule()
sf = SignalFusion()

df = yf.Ticker('SPY').history(period='3mo')
df.columns = [c.lower() for c in df.columns]
sig = tm.score(df, ticker='SPY')
print(f"✅ Technical: direction={sig.direction} confidence={sig.confidence}")

state = sf.fuse([sig])
print(f"✅ Fusion: regime={state.current_regime} composite={state.composite_score}")
```

### 3.4 Watcher Scheduler
```python
# 8 jobs registered
from watcher.scheduler import WatcherScheduler
from watcher.calendar_guard import CalendarGuard
guard = CalendarGuard()
scheduler = WatcherScheduler(calendar=guard)
scheduler.register_default_jobs()
jobs = scheduler.scheduler.get_jobs()
print(f"✅ Jobs registered: {len(jobs)} (expected 8)")
for job in jobs:
    print(f"  - {job.id}: {job.trigger}")
```

### 3.5 Alert Engine
```python
# Discord configured and reachable
from alerts.alert_engine import AlertEngine
engine = AlertEngine()
channels = engine.get_configured_channels()
print(f"✅ Alert channels: {channels}")
```

### 3.6 Paper Account
```python
# Balance and performance
from paper_trading.paper_account import PaperAccount
account = PaperAccount('paper_main')
state = account.state
perf = account.get_performance()
print(f"✅ Paper account: cash=${state.cash:,.0f}")
print(f"   Return: {perf.total_return:.2%}")
print(f"   Open positions: {len(state.open_positions)}")
```

### 3.7 Scanner Watchlist
```python
# Watchlist populated and scores differentiated
from scanner.watchlist_manager import WatchlistManager
mgr = WatchlistManager()
stocks = mgr.get_active()
scores = [s['composite_score'] for s in stocks]
print(f"✅ Watchlist: {len(stocks)} stocks")
print(f"   Score range: {min(scores)}-{max(scores)}")
print(f"   Distinct scores: {len(set(scores))}")
assert len(set(scores)) > 5, "❌ Scores not differentiated"
assert max(scores) > 70, "❌ No high-scoring stocks"
```

### 3.8 Backtest
```python
# NFP backtest accuracy > 65%
import subprocess
result = subprocess.run(
    ['python', 'backtests/nfp_backtest.py'],
    capture_output=True, text=True, cwd='.'
)
# Parse accuracy from output
for line in result.stdout.split('\n'):
    if 'Accuracy' in line:
        print(f"✅ Backtest: {line.strip()}")
```

### 3.9 Sentiment + Geopolitical
```python
# FinBERT loaded, geopolitical detector works
from signals.sentiment_scorer import SentimentScorer
ss = SentimentScorer()
ss.ensure_loaded()
print(f"✅ FinBERT loaded")

# Test geopolitical detection
test_headlines = [
    "US launches military strikes against Iran",
    "Trump announces new tariffs on China",
]
shock = ss.check_geopolitical(test_headlines)
print(f"✅ Geopolitical detector: {shock}")
```

### 3.10 Presidential Monitor
```python
# Trump feed accessible
from signals.sentiment_scorer import SentimentScorer
ss = SentimentScorer()
result = ss.check_presidential()
print(f"✅ Presidential monitor: {result}")
# Should return signal or None (not raise exception)
```

### 3.11 Makefile Commands
```bash
# Verify all make commands exist
make --dry-run start
make --dry-run stop
make --dry-run dashboard
make --dry-run scanner
make --dry-run watchlist
make --dry-run positions
make --dry-run logs
make --dry-run test
make --dry-run healthcheck
make --dry-run test-alerts
make --dry-run earnings      # may not exist yet
make --dry-run analyze       # options analyzer
make --dry-run best-expiry   # options analyzer
```

---

## SECTION 4 — Known Issues Checklist

Verify these previously identified issues are fixed:

```
[ ] WatcherScheduler starts with 8 jobs (was 0)
[ ] Signal pipeline connected to watcher tick
[ ] FinBERT loads on Intel Mac (transformers 4.46.3)
[ ] FRED fallback computes YoY% not raw index
[ ] BLS API rate limiting (5-min TTL cache)
[ ] Sentiment headlines refresh every 15 min (not stale)
[ ] Scanner scores differentiated (not all 68)
[ ] CRWD on watchlist (non-GAAP EPS fix)
[ ] Schwab account_hash cached at init (not per-call)
[ ] Options market orders blocked (require limit_price)
[ ] preview_options_order calls real Schwab API (not stub)
[ ] broker_core/tokens/ exists and gitignored
[ ] PAPER_OPTIONS_COMMISSION removed from settings
[ ] BACKTEST_OPTIONS_COMMISSION_PER_CONTRACT = 0.50
[ ] feedparser installed (was missing)
[ ] Trump Truth Social feed working (trumpstruth.org)
[ ] Negation handling in presidential detector
[ ] Geopolitical detector checks futures before alerting
[ ] NFP backtest accuracy > 65% (Option C scoring)
[ ] BLS_API_KEY wired into BLS connector
[ ] CPI_RELEASE_LABEL accepts YYYY-MM format
```

---

## SECTION 5 — Code Quality Audit

```bash
# Run full test suite
PYTHONPATH=. pytest tests/ --tb=short -q
echo "Expected: 540+ tests passing"

# Linting
ruff check . --statistics
echo "Expected: 0 errors"

# Type checking
mypy config core signals watcher broker_core broker_client
echo "Expected: 0 errors"

# Check for TODO/FIXME/HACK
grep -r "TODO\|FIXME\|HACK\|XXX" . \
  --include="*.py" \
  --exclude-dir=.venv \
  --exclude-dir=__pycache__ \
  | grep -v "test_" \
  | sort
echo "Above are known pending items"

# Check for print() statements (should use logger)
grep -r "^    print\|^print" . \
  --include="*.py" \
  --exclude-dir=.venv \
  --exclude-dir=__pycache__ \
  --exclude-dir=tests \
  | grep -v "dashboard"
echo "Above print() statements should be replaced with logger"
```

---

## SECTION 6 — Security Audit

```bash
# No secrets in code
grep -r "sk-ant-\|SCHWAB_\|Bearer \|password\|secret" . \
  --include="*.py" \
  --exclude-dir=.venv \
  | grep -v "settings.py\|test_\|\.env"
echo "Above should be empty — secrets only in .env"

# .env not committed
git ls-files | grep "^\.env$"
echo "Above should be empty — .env must not be in git"

# Token files not committed  
git ls-files | grep "token.json\|\.token"
echo "Above should be empty — tokens must not be in git"

# .gitignore has critical entries
grep -E "\.env$|token\.json|\.coverage|__pycache__" .gitignore
echo "Above should show all 4 patterns in .gitignore"
```

---

## SECTION 7 — Architecture Compliance

Verify these design decisions are correctly implemented:

### Triple-gate for live trading
```python
# In order_router.py — verify all three must be True
# ENV=live AND DRY_RUN=False AND LIVE_TRADING_ENABLED=True
from broker_client.order_router import OrderRouter
import inspect
source = inspect.getsource(OrderRouter)
assert 'ENV' in source or 'env' in source.lower()
assert 'DRY_RUN' in source or 'dry_run' in source.lower()
assert 'LIVE_TRADING_ENABLED' in source
print("✅ Triple gate present in OrderRouter")
```

### Broker-agnostic design
```python
# BrokerFactory reads BROKER from settings
from broker_core.factory import get_broker
import inspect
source = inspect.getsource(get_broker)
assert 'settings.BROKER' in source or 'BROKER' in source
print("✅ BrokerFactory is config-driven")
```

### No hardcoded values
```python
# Check for hardcoded thresholds in signal files
import subprocess
result = subprocess.run(
    ['grep', '-r', '--include=*.py',
     '-E', r'confidence\s*[><=]+\s*[0-9]+|z\s*[><=]+\s*[0-9]+',
     'signals/', 'broker_client/', 'watcher/'],
    capture_output=True, text=True
)
# Any matches should reference settings.XXX not raw numbers
print("Threshold references (verify all use settings.*):")
print(result.stdout[:2000])
```

---

## SECTION 8 — Generate Final Report

After running all checks, produce this report:

```
╔══════════════════════════════════════════════════════════╗
║          AI-TRADING-SYSTEM VERIFICATION REPORT           ║
╠══════════════════════════════════════════════════════════╣
║  Date: {date}                                            ║
║  Tests: {N} passing                                      ║
╠══════════════════════════════════════════════════════════╣
║  FILES                                                   ║
║  ✅ Present:  {N}                                        ║
║  ❌ Missing:  {N} — list them                            ║
╠══════════════════════════════════════════════════════════╣
║  SETTINGS                                                ║
║  ✅ Configured: {N}                                      ║
║  ❌ Missing:    {N} — list them                          ║
╠══════════════════════════════════════════════════════════╣
║  FUNCTIONAL                                              ║
║  ✅ Passing: {N}/11                                      ║
║  ❌ Failing: {N}/11 — list with errors                   ║
╠══════════════════════════════════════════════════════════╣
║  KNOWN ISSUES                                            ║
║  ✅ Fixed:   {N}/21                                      ║
║  ❌ Pending: {N}/21 — list them                          ║
╠══════════════════════════════════════════════════════════╣
║  CODE QUALITY                                            ║
║  Tests:   {N} passing                                    ║
║  Ruff:    {N} errors                                     ║
║  Mypy:    {N} errors                                     ║
║  TODOs:   {N} remaining                                  ║
║  print(): {N} remaining                                  ║
╠══════════════════════════════════════════════════════════╣
║  SECURITY                                                ║
║  Secrets in code: {N} (should be 0)                     ║
║  .env committed:  {yes/no} (should be no)               ║
║  Tokens committed: {yes/no} (should be no)              ║
╠══════════════════════════════════════════════════════════╣
║  OVERALL STATUS                                          ║
║  🟢 READY FOR PAPER TRADING                              ║
║  🟡 MINOR ISSUES — list                                  ║
║  🔴 CRITICAL ISSUES — list                               ║
╚══════════════════════════════════════════════════════════╝

CRITICAL ISSUES (must fix before next market session):
  1. ...

MINOR ISSUES (fix this week):
  1. ...

PENDING FEATURES (Phase 2+):
  1. ...
```

---

## Notes for Claude Code

- Run ALL sections in order
- Do NOT skip sections even if earlier ones pass
- For any FAIL — show the actual error, not just "failed"
- For missing files — check if functionality exists elsewhere
- For settings — check both settings.py AND .env.example
- The goal is an honest complete picture, not a passing grade
- After the report — ask: "Fix all CRITICAL issues now?"