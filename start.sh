#!/usr/bin/env bash
# start.sh — launch trading engine + dashboard
# Usage: ./start.sh
# Ctrl+C shuts down both processes cleanly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Activate virtualenv ───────────────────────────────────────────────────────
if [[ ! -f ".venv/bin/activate" ]]; then
    echo "[start.sh] ERROR: .venv not found. Run: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── Ensure logs dir exists ────────────────────────────────────────────────────
mkdir -p logs

# ── Read dashboard port from .env (default 8501) ─────────────────────────────
DASHBOARD_PORT=8501
if [[ -f ".env" ]]; then
    _port=$(grep -E "^DASHBOARD_PORT=" .env | cut -d= -f2 | tr -d '[:space:]')
    [[ -n "$_port" ]] && DASHBOARD_PORT="$_port"
fi

# ── Cleanup handler ───────────────────────────────────────────────────────────
TRADING_PID=""

cleanup() {
    echo ""
    echo "[start.sh] Shutting down..."
    if [[ -n "$TRADING_PID" ]] && kill -0 "$TRADING_PID" 2>/dev/null; then
        kill "$TRADING_PID"
        wait "$TRADING_PID" 2>/dev/null || true
        echo "[start.sh] Trading engine stopped (pid $TRADING_PID)"
    fi
    echo "[start.sh] Done."
    exit 0
}

trap cleanup INT TERM

# ── Start trading engine in background ───────────────────────────────────────
echo "[start.sh] Starting trading engine → logs/trading.log"
PYTHONPATH="$SCRIPT_DIR" python main.py >> logs/trading.log 2>&1 &
TRADING_PID=$!
echo "[start.sh] Trading engine pid: $TRADING_PID"

# Give the engine a moment to fail fast on bad config before launching dashboard
sleep 2
if ! kill -0 "$TRADING_PID" 2>/dev/null; then
    echo "[start.sh] ERROR: Trading engine exited immediately. Check logs/trading.log"
    tail -20 logs/trading.log
    exit 1
fi

# ── Start Streamlit dashboard in foreground ───────────────────────────────────
echo "[start.sh] Starting dashboard on http://localhost:${DASHBOARD_PORT}"
PYTHONPATH="$SCRIPT_DIR" streamlit run dashboard/app.py \
    --server.port "$DASHBOARD_PORT" \
    --server.headless true \
    --browser.gatherUsageStats false

# If streamlit exits on its own, clean up the trading engine too
cleanup
