## 18. BUILD ORDER — DAY BY DAY

### Day 1 AM — Project scaffold + server
- Full folder structure (all dirs + `__init__.py`)
- `requirements.txt` (Python 3.12.7)
- `pyproject.toml` (black + ruff + mypy)
- `.env.example` (all keys documented)
- `config/settings.py` (full pydantic-settings)
- `core/logger.py`, `core/exceptions.py`, `core/retry.py`
- DigitalOcean Ubuntu 24 bash setup script
- `systemd` service file
- Tests for core modules

### Day 1 PM — Data layer
- `data/data_manager.py` (unified DataManager)
- `data/yfinance_connector.py` (OHLCV + SQLite cache)
- `data/alpaca_connector.py` (real-time quotes)
- `data/fred_connector.py` (macro data)
- `data/edgar_connector.py` (edgartools wrapper)
- SQLite schema initialisation
- Tests + integration test against real APIs

### Day 2 — Macro engine + yields
- `signals/signal_schema.py` (Signal, MarketState, MarketSnapshot)
- `signals/macro_engine.py` (NFP/CPI surprise scorer)
- `signals/yield_monitor.py` (10yr yield delta)
- Tests: replay 3 historical NFP days

### Day 3 — NLP + pre-market
- `signals/sentiment_scorer.py` (FinBERT + NewsAPI + RSS)
- `signals/premarket_watcher.py` (NQ/ES, BTC, FedWatch)
- Tests: mock news API, validate FinBERT output

### Day 4 — Technical + signal fusion
- `signals/technical_module.py` (pandas-ta: RSI, MACD, Bollinger)
- `signals/signal_fusion.py` (weighted ensemble)
- `watcher/market_state.py` (regime tracker, transition-only alerts)
- Tests: fusion with all combinations of signal inputs

### Day 5 — Watcher + alerts
- `watcher/scheduler.py` (APScheduler, all phases)
- `watcher/calendar_guard.py` (pandas_market_calendars)
- `alerts/alert_engine.py` (Twilio + Slack + SendGrid)
- `alerts/accuracy_logger.py`
- Tests: mock all alert channels

### Day 6 — Broker core + order router
- `broker_core/base_broker.py` (BaseBroker + all pydantic models)
- `broker_core/schwab_broker.py` (schwab-py, all 14 methods + `preview_order`)
- `broker_core/alpaca_broker.py` (alpaca-py, all 14 methods)
- `broker_core/factory.py` (BrokerFactory)
- `broker_client/order_router.py` (three-mode routing)
- Tests: mock broker, test all three routing paths

### Day 7 — Paper trading engine
- `paper_trading/paper_account.py` (PaperAccount, thread-safe JSON)
- `paper_trading/paper_engine.py` (simulate_fill, all three methods)
- `paper_trading/performance.py` (Sharpe, Sortino, drawdown, win rate)
- Tests: simulate 10 trades, validate P&L calculations

### Day 8 — Strategies + trading engine
- `broker_client/strategies/base_strategy.py`
- `broker_client/strategies/equity_long_short.py`
- `broker_client/strategies/momentum_breakout.py`
- `broker_client/strategies/__init__.py` (registry)
- `broker_client/trading_engine.py`
- `broker_client/risk_manager.py` (Kelly, daily loss limit)
- `broker_client/order_manager.py`
- Tests: end-to-end signal → strategy → router → paper account

### Day 9 — Position watcher
- `broker_client/position_manager.py`
- `broker_client/position_watcher.py` (startup load, regime-aware, auto-register)
- Tests: startup reconciliation, stop-loss trigger, regime change closure

### Day 10 — Options strategies + engine
- `broker_client/options_engine.py`
- `broker_client/strategies/covered_call.py`
- `broker_client/strategies/cash_secured_put.py`
- `broker_client/strategies/protective_put.py`
- `broker_client/strategies/iron_condor.py`
- Tests: chain fetch mock, strike selection logic

### Day 11 — Backtest + validation
- `backtests/nfp_backtest.py` (VectorBT, replay 5yr NFP days)
- Run backtest, validate V1 direction accuracy > 65%
- Tune signal fusion weights based on results

### Day 12 — Streamlit dashboard
- `dashboard/app.py` (multi-page entry)
- `dashboard/pages/1_overview.py` (paper vs live, equity curve vs SPY)
- `dashboard/pages/2_positions.py` (live P&L table)
- `dashboard/pages/3_performance.py` (metrics, trade log)
- `dashboard/pages/4_account_mgmt.py` (balance init, adjustments)

### Days 13–18 — Multi-bagger scanner (Stages 1–5 + watchlist manager)

### Days 19–25 — V2 (GEX + EGARCH + live execution)

### Days 26–30 — Production hardening
- GitHub Actions CI (pytest on every push)
- Log rotation
- `/health` endpoint for UptimeRobot
- Runbook (`docs/runbook.md`)
- Paper trading begins — minimum 3 months before live capital

---