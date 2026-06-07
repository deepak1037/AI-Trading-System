"""System healthcheck — tests every major component and prints a pass/fail table.

Usage:
    python scripts/healthcheck.py
    make healthcheck
"""

from __future__ import annotations

import importlib
import os
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

# Ensure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TIMEOUT_SECONDS = 10


@contextmanager
def _timeout(seconds: int):
    """Raise TimeoutError after `seconds`."""
    def _handler(signum, frame):
        raise TimeoutError(f"Timed out after {seconds}s")
    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@dataclass
class Result:
    name: str
    passed: bool
    detail: str = ""
    error: str = ""


results: list[Result] = []


def check(name: str):
    """Decorator to register a healthcheck."""
    def _wrapper(fn):
        try:
            with _timeout(TIMEOUT_SECONDS):
                detail = fn()
            results.append(Result(name=name, passed=True, detail=str(detail or "")))
        except Exception as exc:
            tb = traceback.format_exc().strip().splitlines()
            short_err = tb[-1] if tb else str(exc)
            results.append(Result(name=name, passed=False, error=short_err))
        return fn
    return _wrapper


# ─────────────────────────────────────────────────────────────────────────────
# 1. Config
# ─────────────────────────────────────────────────────────────────────────────
@check("Config — settings load")
def _check_config():
    from config.settings import settings
    return f"ENV={settings.ENV} BROKER={settings.BROKER}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Database
# ─────────────────────────────────────────────────────────────────────────────
@check("Database — connect + query tables")
def _check_db():
    import sqlite3
    from config.settings import settings
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    with sqlite3.connect(settings.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    tables = [r[0] for r in rows]
    return f"{len(tables)} tables"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Logging
# ─────────────────────────────────────────────────────────────────────────────
@check("Logging — get_logger initialises")
def _check_logging():
    from core.logger import get_logger
    logger = get_logger("healthcheck")
    logger.debug("healthcheck test message")
    return "OK"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Broker — auth
# ─────────────────────────────────────────────────────────────────────────────
@check("Broker (Schwab) — auth + get_account")
def _check_broker_auth():
    from broker_core.factory import get_broker
    broker = get_broker()
    account = broker.get_account()
    last4 = account.account_id[-4:] if account.account_id else "????"
    return f"...{last4}  cash=${account.cash:,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Broker — quote
# ─────────────────────────────────────────────────────────────────────────────
@check("Broker (Schwab) — quote (AAPL)")
def _check_broker_quote():
    from broker_core.factory import get_broker
    broker = get_broker()
    quote = broker.get_quote("AAPL")
    return f"${quote.last:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Broker — options chain
# ─────────────────────────────────────────────────────────────────────────────
@check("Broker (Schwab) — options chain (AAPL)")
def _check_broker_options():
    from broker_core.factory import get_broker
    broker = get_broker()
    chain = broker.get_options_chain("AAPL")
    n_calls = len(chain.calls)
    n_puts = len(chain.puts)
    return f"{n_calls} calls, {n_puts} puts  expiry={chain.expiry}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Data — yfinance OHLCV
# ─────────────────────────────────────────────────────────────────────────────
@check("Data — yfinance OHLCV (AAPL, 5 days)")
def _check_yfinance():
    import yfinance as yf
    try:
        hist = yf.Ticker("AAPL").history(period="5d")
    except Exception as exc:
        err_str = str(exc)
        if "RateLimit" in err_str or "Too Many Requests" in err_str or "rate limit" in err_str.lower():
            return "RATE LIMITED (transient — retry later)"
        raise
    if hist.empty:
        raise RuntimeError("Empty dataframe returned")
    return f"{len(hist)} rows  last close=${hist['Close'].iloc[-1]:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Data — FRED API
# ─────────────────────────────────────────────────────────────────────────────
@check("Data — FRED API responds")
def _check_fred():
    import requests
    from config.settings import settings
    if not settings.FRED_API_KEY:
        return "SKIPPED (no FRED_API_KEY)"
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=GS10&api_key={settings.FRED_API_KEY}&limit=1&file_type=json"
    )
    resp = requests.get(url, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    val = obs[-1]["value"] if obs else "?"
    return f"10yr yield last obs={val}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. Data — EDGAR recent filings
# ─────────────────────────────────────────────────────────────────────────────
@check("Data — EDGAR recent filings (AAPL)")
def _check_edgar():
    try:
        import edgartools as et  # type: ignore[import-untyped]
        company = et.Company("AAPL")
        filings = company.get_filings(form="10-K").head(1)
        count = len(filings)
        return f"{count} 10-K filing(s) found"
    except ImportError:
        # edgartools may not be installed in all environments
        import requests
        resp = requests.get(
            "https://data.sec.gov/submissions/CIK0000320193.json",
            headers={"User-Agent": "AI-Trading-System healthcheck@example.com"},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        name = resp.json().get("name", "?")
        return f"SEC EDGAR direct: {name}"


# ─────────────────────────────────────────────────────────────────────────────
# 10–15. Signals — all six modules initialise
# ─────────────────────────────────────────────────────────────────────────────
@check("Signals — MacroEngine initialises")
def _check_macro():
    from signals.macro_engine import MacroEngine
    MacroEngine()
    return "OK"


@check("Signals — YieldMonitor initialises")
def _check_yield():
    from signals.yield_monitor import YieldMonitor
    YieldMonitor()
    return "OK"


@check("Signals — SentimentScorer initialises")
def _check_sentiment():
    from signals.sentiment_scorer import SentimentScorer
    SentimentScorer()
    return "OK"


@check("Signals — PremarketWatcher initialises")
def _check_premarket():
    from signals.premarket_watcher import PremarketWatcher
    PremarketWatcher()
    return "OK"


@check("Signals — TechnicalModule initialises")
def _check_technical():
    from signals.technical_module import TechnicalModule
    TechnicalModule()
    return "OK"


@check("Signals — SignalFusion initialises")
def _check_fusion():
    from signals.signal_fusion import SignalFusion
    SignalFusion()
    return "OK"


# ─────────────────────────────────────────────────────────────────────────────
# 16. Watcher — Scheduler
# ─────────────────────────────────────────────────────────────────────────────
@check("Watcher — Scheduler initialises")
def _check_scheduler():
    from watcher.calendar_guard import CalendarGuard
    from watcher.scheduler import WatcherScheduler
    guard = CalendarGuard()
    WatcherScheduler(calendar=guard)
    return "OK"


# ─────────────────────────────────────────────────────────────────────────────
# 17. Watcher — CalendarGuard
# ─────────────────────────────────────────────────────────────────────────────
@check("Watcher — CalendarGuard detects today")
def _check_calendar():
    from watcher.calendar_guard import CalendarGuard
    guard = CalendarGuard()
    is_trading_day = guard.is_trading_day()
    is_open_now = guard.is_market_open()
    hours = guard.market_hours()
    hours_str = f"{hours[0].strftime('%H:%M')}-{hours[1].strftime('%H:%M')} ET" if hours else "closed today"
    return f"trading_day={is_trading_day}  open_now={is_open_now}  {hours_str}"


# ─────────────────────────────────────────────────────────────────────────────
# 18–19. Paper account
# ─────────────────────────────────────────────────────────────────────────────
@check("Paper account — loads paper_main.json")
def _check_paper_load():
    from paper_trading.paper_account import PaperAccount
    acct = PaperAccount("paper_main")
    state = acct.get_state()
    return f"cash=${state.cash:,.2f}  equity=${state.equity:,.2f}"


@check("Paper account — get_performance() returns data")
def _check_paper_perf():
    from paper_trading.paper_account import PaperAccount
    acct = PaperAccount("paper_main")
    perf = acct.get_performance()
    return (
        f"return={perf.total_return_pct:+.2f}%  "
        f"open={perf.open_positions_count}  "
        f"closed={perf.closed_positions_count}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 20. Scanner — WatchlistManager
# ─────────────────────────────────────────────────────────────────────────────
@check("Scanner — WatchlistManager initialises")
def _check_watchlist():
    from scanner.watchlist_manager import WatchlistManager
    wm = WatchlistManager()
    active = wm.get_active()
    return f"{len(active)} active watchlist entries"


# ─────────────────────────────────────────────────────────────────────────────
# 21. Dashboard — all page files exist and import
# ─────────────────────────────────────────────────────────────────────────────
@check("Dashboard — page files exist and import cleanly")
def _check_dashboard():
    import ast
    pages_dir = os.path.join(ROOT, "dashboard", "pages")
    expected = [
        "1_overview.py",
        "2_positions.py",
        "3_performance.py",
        "4_account_mgmt.py",
        "5_orders.py",
    ]
    missing = []
    parse_errors = []
    for fname in expected:
        fpath = os.path.join(pages_dir, fname)
        if not os.path.exists(fpath):
            missing.append(fname)
        else:
            try:
                with open(fpath) as f:
                    ast.parse(f.read(), filename=fpath)
            except SyntaxError as e:
                parse_errors.append(f"{fname}: {e}")
    if missing:
        raise FileNotFoundError(f"Missing pages: {', '.join(missing)}")
    if parse_errors:
        raise SyntaxError("; ".join(parse_errors))
    return f"{len(expected)} pages OK"


# ─────────────────────────────────────────────────────────────────────────────
# 22. Alerts — AlertEngine
# ─────────────────────────────────────────────────────────────────────────────
@check("Alerts — AlertEngine initialises (no credentials)")
def _check_alerts():
    from alerts.alert_engine import AlertEngine
    from config.settings import settings
    AlertEngine()  # must not raise
    channels = []
    if all([settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN,
            settings.TWILIO_FROM_NUMBER, settings.TWILIO_TO_NUMBER]):
        channels.append("twilio")
    if settings.SLACK_WEBHOOK_URL:
        channels.append("slack")
    if settings.SENDGRID_API_KEY:
        channels.append("sendgrid")
    return f"channels: {', '.join(channels) or 'none configured (OK)'}"


# ─────────────────────────────────────────────────────────────────────────────
# 23. Position watcher
# ─────────────────────────────────────────────────────────────────────────────
@check("Position watcher — initialises correctly")
def _check_position_watcher():
    from broker_client.order_router import OrderRouter
    from broker_client.position_manager import PositionManager
    from broker_client.position_watcher import PositionWatcher
    from broker_core.factory import get_broker
    broker = get_broker()
    router = OrderRouter(broker=broker)
    pm = PositionManager()
    watcher = PositionWatcher(broker=broker, router=router, position_manager=pm)
    n = len(watcher.positions)
    return f"{n} positions loaded"


# ─────────────────────────────────────────────────────────────────────────────
# 24. Trading engine
# ─────────────────────────────────────────────────────────────────────────────
@check("Trading engine — initialises with enabled strategies")
def _check_trading_engine():
    from broker_client.order_manager import OrderManager
    from broker_client.order_router import OrderRouter
    from broker_client.risk_manager import RiskManager
    from broker_client.trading_engine import TradingEngine
    from broker_core.factory import get_broker
    from config.settings import settings
    broker = get_broker()
    account = broker.get_account()
    router = OrderRouter(broker=broker)
    risk = RiskManager()
    om = OrderManager()
    engine = TradingEngine(
        router=router,
        risk_manager=risk,
        order_manager=om,
        account=account,
    )
    n = len(engine._strategies)
    names = [s.strategy_name for s in engine._strategies]
    return f"{n} strategies: {', '.join(names) or settings.ENABLED_STRATEGIES}"


# ─────────────────────────────────────────────────────────────────────────────
# Print results table
# ─────────────────────────────────────────────────────────────────────────────

def _print_table(results: list[Result]) -> None:
    COL_NAME = 36
    COL_STATUS = 8
    COL_DETAIL = 40

    top    = f"┌{'─'*(COL_NAME+2)}┬{'─'*(COL_STATUS+2)}┬{'─'*(COL_DETAIL+2)}┐"
    header = f"│ {'Component':<{COL_NAME}} │ {'Status':<{COL_STATUS}} │ {'Detail':<{COL_DETAIL}} │"
    sep    = f"├{'─'*(COL_NAME+2)}┼{'─'*(COL_STATUS+2)}┼{'─'*(COL_DETAIL+2)}┤"
    bot    = f"└{'─'*(COL_NAME+2)}┴{'─'*(COL_STATUS+2)}┴{'─'*(COL_DETAIL+2)}┘"

    print(top)
    print(header)
    print(sep)

    for r in results:
        status_str = "✓ PASS" if r.passed else "✗ FAIL"
        detail_raw = r.detail if r.passed else r.error
        # Truncate detail to fit column
        detail = detail_raw[:COL_DETAIL] if detail_raw else ""
        name = r.name[:COL_NAME]
        print(f"│ {name:<{COL_NAME}} │ {status_str:<{COL_STATUS}} │ {detail:<{COL_DETAIL}} │")

    print(bot)
    print()

    failures = [r for r in results if not r.passed]
    passed = len(results) - len(failures)
    print(f"PASSED: {passed}/{len(results)}")
    if failures:
        print(f"FAILED: {len(failures)}/{len(results)}")
        print()
        for r in failures:
            print(f"  ✗ {r.name}")
            # Print full traceback-style error
            for line in r.error.splitlines():
                print(f"      {line}")
            print()


if __name__ == "__main__":
    start = time.time()
    _print_table(results)
    elapsed = time.time() - start
    print(f"Completed in {elapsed:.1f}s")
    # Exit with non-zero if any check failed
    sys.exit(0 if all(r.passed for r in results) else 1)
