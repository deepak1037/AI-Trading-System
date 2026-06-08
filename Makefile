SHELL := /bin/bash

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT        := $(shell pwd)
VENV        := $(ROOT)/.venv
PYTHON      := $(VENV)/bin/python
STREAMLIT   := $(VENV)/bin/streamlit
PYTEST      := $(VENV)/bin/pytest
RUFF        := $(VENV)/bin/ruff
MYPY        := $(VENV)/bin/mypy
LOG_FILE    := logs/trading.log
PID_MAIN    := .pids/main.pid
PID_DASH    := .pids/dashboard.pid

export PYTHONPATH := $(ROOT)

# ── Helpers ───────────────────────────────────────────────────────────────────
.PHONY: _check_venv
_check_venv:
	@test -f $(PYTHON) || (echo "ERROR: .venv not found. Run: python -m venv .venv && pip install -r requirements.txt" && exit 1)

.PHONY: _mkpids _mklogs
_mkpids:
	@mkdir -p .pids

_mklogs:
	@mkdir -p logs

# ── start: trading engine in background ──────────────────────────────────────
.PHONY: start
start: _check_venv _mkpids _mklogs
	@if [ -f $(PID_MAIN) ] && kill -0 $$(cat $(PID_MAIN)) 2>/dev/null; then \
		echo "Trading engine already running (pid $$(cat $(PID_MAIN)))"; \
	else \
		$(PYTHON) main.py >> $(LOG_FILE) 2>&1 & echo $$! > $(PID_MAIN); \
		echo "Trading engine started (pid $$(cat $(PID_MAIN))) → $(LOG_FILE)"; \
	fi

# ── dashboard: Streamlit in foreground ───────────────────────────────────────
.PHONY: dashboard
dashboard: _check_venv
	@echo "Stopping any running Streamlit..."
	@pkill -f streamlit || true
	@$(STREAMLIT) run dashboard/app.py \
		--server.port $$(grep -E "^DASHBOARD_PORT=" .env 2>/dev/null | cut -d= -f2 || echo 8501) \
		--server.headless true \
		--browser.gatherUsageStats false

# ── all: trading engine background + dashboard foreground ────────────────────
.PHONY: all
all: _check_venv _mkpids _mklogs
	@$(MAKE) --no-print-directory start
	@sleep 2
	@if [ -f $(PID_MAIN) ] && ! kill -0 $$(cat $(PID_MAIN)) 2>/dev/null; then \
		echo "ERROR: Trading engine crashed. Check $(LOG_FILE):"; \
		tail -20 $(LOG_FILE); \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory dashboard

# ── stop: kill both processes ─────────────────────────────────────────────────
.PHONY: stop
stop:
	@stopped=0; \
	if [ -f $(PID_MAIN) ]; then \
		pid=$$(cat $(PID_MAIN)); \
		if kill -0 $$pid 2>/dev/null; then kill $$pid && echo "Trading engine stopped (pid $$pid)"; stopped=1; fi; \
		rm -f $(PID_MAIN); \
	fi; \
	if [ -f $(PID_DASH) ]; then \
		pid=$$(cat $(PID_DASH)); \
		if kill -0 $$pid 2>/dev/null; then kill $$pid && echo "Dashboard stopped (pid $$pid)"; stopped=1; fi; \
		rm -f $(PID_DASH); \
	fi; \
	pids=$$(pgrep -f "streamlit run dashboard" 2>/dev/null); \
	if [ -n "$$pids" ]; then kill $$pids && echo "Streamlit stopped ($$pids)"; stopped=1; fi; \
	pids=$$(pgrep -f "python main.py" 2>/dev/null); \
	if [ -n "$$pids" ]; then kill $$pids && echo "main.py stopped ($$pids)"; stopped=1; fi; \
	[ $$stopped -eq 0 ] && echo "No running processes found."

# ── scanner: full 5-stage multi-bagger funnel ────────────────────────────────
.PHONY: scanner
scanner: _check_venv _mklogs
	@echo "Running multi-bagger scanner (Stages 1-5)..."
	@$(PYTHON) scripts/run_scanner.py 2>&1 | tee -a $(LOG_FILE)

# ── build-watchlist: reliable curated-universe watchlist (recommended) ───────
.PHONY: build-watchlist
build-watchlist: _check_venv _mklogs
	@$(PYTHON) scripts/build_watchlist.py 2>&1 | tee -a $(LOG_FILE)

# ── watchlist: print current watchlist from SQLite ───────────────────────────
.PHONY: watchlist
watchlist: _check_venv
	@$(PYTHON) -c "\
import sqlite3, os; \
db = os.environ.get('DB_PATH', 'db/trading.db'); \
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row; \
rows = conn.execute('SELECT ticker, name, composite_score, added_at FROM watchlist WHERE is_active=1 ORDER BY composite_score DESC').fetchall(); \
conn.close(); \
print(f'{'─'*60}'); print(f'  WATCHLIST  ({len(rows)} stocks)'); print(f'{'─'*60}'); \
print(f'  {'TICKER':<10} {'SCORE':>5}   ADDED'); \
print(f'{'─'*60}'); \
[print(f\"  {r['ticker']:<10} {r['composite_score']:>5}   {r['added_at'][:10]}\") for r in rows] or print('  (empty)'); \
print(f'{'─'*60}') \
"

# ── positions: print Schwab + paper positions ─────────────────────────────────
.PHONY: positions
positions: _check_venv
	@$(PYTHON) -c "\
from broker_core.factory import get_broker; \
from config.settings import settings; \
import sqlite3; \
print(); print('=== BROKER POSITIONS ($(BROKER)) ==='); \
try: \
    b = get_broker(); ps = b.get_positions(); \
    [print(f'  {p.ticker:<30} qty={p.qty:>6}  entry=\$${p.avg_cost:>8.2f}  pnl=\$${p.unrealized_pnl:>+10.2f}') for p in ps] or print('  (none)'); \
except Exception as e: print(f'  Error: {e}'); \
print(); print('=== PAPER POSITIONS (paper_main) ==='); \
import json; from pathlib import Path; \
path = Path(settings.PAPER_ACCOUNTS_DIR) / 'paper_main.json'; \
data = json.loads(path.read_text()) if path.exists() else {}; \
ps = data.get('open_positions', []); \
[print(f\"  {p['ticker']:<30} qty={p.get('qty',0):>6}  entry=\$${p.get('entry_price',0):>8.2f}\") for p in ps] or print('  (none)'); \
print() \
" 2>/dev/null

# ── status: running processes + last 10 log lines ────────────────────────────
.PHONY: status
status:
	@echo ""
	@echo "=== PROCESSES ==="
	@if [ -f $(PID_MAIN) ] && kill -0 $$(cat $(PID_MAIN)) 2>/dev/null; then \
		echo "  trading engine  RUNNING  (pid $$(cat $(PID_MAIN)))"; \
	else echo "  trading engine  STOPPED"; fi
	@pids=$$(pgrep -f "streamlit run dashboard" 2>/dev/null); \
	if [ -n "$$pids" ]; then echo "  dashboard       RUNNING  (pid $$pids)"; \
	else echo "  dashboard       STOPPED"; fi
	@echo ""
	@echo "=== LAST 10 LOG LINES ==="
	@if [ -f $(LOG_FILE) ]; then tail -10 $(LOG_FILE); else echo "  (no log file yet)"; fi
	@echo ""

# ── logs: tail trading log ────────────────────────────────────────────────────
.PHONY: logs
logs: _mklogs
	@echo "Tailing $(LOG_FILE) — Ctrl+C to stop"
	@tail -f $(LOG_FILE)

# ── test: run pytest ──────────────────────────────────────────────────────────
.PHONY: test
test: _check_venv
	@$(PYTEST) tests/ --tb=short -q

# ── lint: ruff + mypy ─────────────────────────────────────────────────────────
.PHONY: lint
lint: _check_venv
	@echo "--- ruff ---"
	@$(RUFF) check . --select E,F --ignore E501 --exclude .venv
	@echo "--- mypy ---"
	@$(MYPY) config core signals watcher alerts broker_core broker_client paper_trading \
		--ignore-missing-imports \
		--disable-error-code=unused-ignore \
		--disable-error-code=misc \
		--disable-error-code=no-untyped-def \
		|| true

# ── healthcheck: test every system component ─────────────────────────────────
.PHONY: healthcheck
healthcheck: _check_venv _mklogs
	@$(PYTHON) scripts/healthcheck.py

# ── test-alerts: send a test embed to each Discord channel ───────────────────
.PHONY: test-alerts
test-alerts: _check_venv
	@$(PYTHON) scripts/test_alerts.py

# ── analyze: LLM-augmented put ROI analysis ──────────────────────────────────
#   make analyze ticker=HOOD strike=8 expiry=2026-07-18
.PHONY: analyze
analyze: _check_venv _mklogs
	@test -n "$(ticker)" || (echo "Usage: make analyze ticker=HOOD strike=8 expiry=2026-07-18" && exit 1)
	@test -n "$(strike)" || (echo "Usage: make analyze ticker=HOOD strike=8 expiry=2026-07-18" && exit 1)
	@test -n "$(expiry)" || (echo "Usage: make analyze ticker=HOOD strike=8 expiry=2026-07-18" && exit 1)
	@$(PYTHON) scripts/analyze_put.py --ticker $(ticker) --strike $(strike) --expiry $(expiry)

# ── paper-balance: paper account summary ─────────────────────────────────────
.PHONY: paper-balance
paper-balance: _check_venv
	@$(PYTHON) -c "\
from paper_trading.paper_account import PaperAccount; \
from config.settings import settings; \
for acct_id in settings.PAPER_ACCOUNTS: \
    a = PaperAccount(acct_id); s = a.get_state(); p = a.get_performance(); \
    print(); print(f'=== {acct_id.upper()} ==='); \
    print(f'  Cash:          \$${s.cash:>12,.2f}'); \
    print(f'  Equity:        \$${s.equity:>12,.2f}'); \
    print(f'  Open positions: {len(s.open_positions):>3}'); \
    print(f'  Total return:   {p.total_return_pct:>+.2f}%'); \
    print(f'  Sharpe:         {p.sharpe_ratio:>+.2f}'); \
    print(f'  Max drawdown:   {p.max_drawdown_pct:>.2f}%'); \
    print(f'  Win rate:       {p.win_rate:.1%}'); \
    print(f'  Trades:         {p.total_trades:>3}'); \
print() \
"
