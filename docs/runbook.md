# AI Trading System — Operations Runbook

## Quick Start

```bash
# 1. Clone and install
git clone <repo>
cd AI-Trading-System
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your API keys and settings

# 3. Run in development mode
ENV=development python main.py

# 4. Run paper trading
ENV=paper BROKER=schwab python main.py

# 5. Start dashboard
streamlit run dashboard/app.py --server.port 8501
```

---

## Environment Modes

| ENV | What happens |
|---|---|
| `development` | DEBUG logging, no orders, no broker calls |
| `backtest` | Internal PaperEngine, offline simulation |
| `paper` | Schwab previewOrder API, PaperAccount updated |
| `live` | **Real money** — requires all three: `ENV=live DRY_RUN=False LIVE_TRADING_ENABLED=True` |

**NEVER** go live without:
1. Minimum 3 months of paper trading
2. Direction accuracy > 65% on real paper trades (not just backtest)
3. Maximum drawdown < 10% over the paper period

---

## systemd Service

```bash
# Install
sudo cp trading-system.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-system
sudo systemctl start trading-system

# Check status
sudo systemctl status trading-system
sudo journalctl -u trading-system -f

# Restart
sudo systemctl restart trading-system
```

---

## Health Check

```
GET http://server-ip:8080/health
```

Expected response:
```json
{
  "status": "ok",
  "env": "paper",
  "broker": "schwab",
  "dry_run": true,
  "live_trading_enabled": false,
  "uptime_seconds": 3600,
  "timestamp": "2026-06-07T..."
}
```

Configure UptimeRobot to ping `:8080/health` every 5 minutes.

---

## Log Files

| File | Contents | Rotation |
|---|---|---|
| `logs/trading.log` | All application logs | 10 MB max, 5 backups |

View live logs:
```bash
tail -f logs/trading.log
# or filter by level
grep ERROR logs/trading.log | tail -20
```

---

## Paper Account Management

```python
from paper_trading.paper_account import PaperAccount
from config.settings import settings

acct = PaperAccount("paper_main")

# Initialize
acct.initialize_balance(50000.0, note="Starting paper trading 2026-06-07")

# Check state
state = acct.get_state()
print(f"Equity: ${state.equity:,.2f}")
print(f"Open positions: {len(state.open_positions)}")

# Get performance
perf = acct.get_performance()
print(f"Return: {perf.total_return_pct:.2f}%")

# Reset (keeps trade history)
acct.reset(confirm=True, new_balance=50000.0)
```

---

## Signal Accuracy Review

```python
from alerts.accuracy_logger import AccuracyLogger
logger = AccuracyLogger()

# Fill EOD actuals (run after 4:15 PM)
logger.fill_actual("2026-06-07")

# Get 30-day accuracy
accuracy = logger.get_accuracy(days=30)
print(f"Direction accuracy: {accuracy}")
```

---

## NFP Backtest

```bash
python backtests/nfp_backtest.py
# Expected: 79.3% accuracy at min_z=1.0 — PASS >65% target
```

---

## Troubleshooting

### System won't start
1. Check `.env` file exists and has required keys
2. Run `python -c "from config.settings import settings; print(settings)"` to validate config
3. Check `db/trading.db` is writable

### Orders not executing
1. Verify `ENV`, `DRY_RUN`, and `LIVE_TRADING_ENABLED` triple-gate
2. Check broker API keys in `.env`
3. Look for `BrokerError` in logs

### Signal accuracy degrading
1. Check `accuracy_log` table in SQLite for recent predictions
2. Re-run NFP backtest: `python backtests/nfp_backtest.py`
3. Review signal fusion weights in `config/settings.py`

### High memory / CPU
1. Check APScheduler job count: `scheduler.get_jobs()`
2. Position watcher scan interval: `POSITION_SCAN_INTERVAL_SECONDS` in `.env`
3. Check for memory leaks in `logs/trading.log`

---

## Emergency Stop

```bash
# Stop the systemd service
sudo systemctl stop trading-system

# Close all positions manually (if in paper mode)
python -c "
from paper_trading.paper_account import PaperAccount
a = PaperAccount('paper_main')
s = a.get_state()
print('Open positions:', len(s.open_positions))
for p in s.open_positions:
    print(p.ticker, p.qty, p.entry_price)
"
```

---

## Deployment (DigitalOcean Ubuntu 24)

```bash
# Run the setup script
bash scripts/setup_server.sh

# Or manually:
apt update && apt install -y python3.12 python3.12-venv git
git clone <repo> /opt/trading-system
cd /opt/trading-system
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env
sudo systemctl enable trading-system
sudo systemctl start trading-system
```

Server: DigitalOcean $12/mo Ubuntu 24 (2 vCPU, 2GB RAM)

---

## Paper Trading Checklist (before going live)

- [ ] Paper traded for minimum 90 days
- [ ] Direction accuracy ≥ 65% on 50+ real paper signals
- [ ] Max daily loss never exceeded 2% limit
- [ ] No uncaught exceptions in 30 days of logs
- [ ] Schwab `previewOrder` confirmed working on paper account
- [ ] Position watcher tested: stop-loss and take-profit fill correctly
- [ ] Alert channels tested: SMS, Slack, email all delivering
- [ ] Dashboard showing accurate P&L and equity curve
- [ ] Drawdown < 10% over paper period
- [ ] Backtest accuracy confirmed ≥ 65% on held-out data

Only after ALL items checked: change `ENV=live DRY_RUN=False LIVE_TRADING_ENABLED=True`.
