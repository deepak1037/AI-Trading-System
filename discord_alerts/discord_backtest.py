"""discord_alerts/discord_backtest.py

Replay real historical Ravish Discord alerts through the paper account
simulator — as if the listener had been running since a given date.

What it does:
  1. Fetches ALL messages from signal channel + income-trades forum
     back to --from date via Discord REST API (paginated with ?before=).
  2. Runs each message through the same DiscordSignalParser used by the
     live listener — no code duplication.
  3. Runs exit-keyword detection (Ravish "all out / closed" messages).
  4. For positions that never got an explicit close message:
       a. If expiry_short is in the past → fetch underlying close price on
          expiry date from yfinance → determine expired worthless or loss.
       b. If DTE hit the exit_rules.yaml dte_stop threshold before expiry →
          close at the rule's profit_target_pct (conservative).
       c. If still open → left as open (cost tied up, no P&L yet).
  5. Simulates the $100k paper account with MSP 2× margin buffer.
  6. Prints per-trade table + monthly breakdown + final P&L summary.

Usage:
    python discord_alerts/discord_backtest.py
    python discord_alerts/discord_backtest.py --from 2026-09-01
    python discord_alerts/discord_backtest.py --from 2026-08-01 --balance 100000
    python discord_alerts/discord_backtest.py --from 2026-09-01 --multiplier 1.5 --verbose

Requires:
    DISCORD_USER_TOKEN  in .env
    pip install yfinance requests pyyaml
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import requests
import yaml

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from discord_alerts.discord_signal_reader import AlertParser, ParsedSignal
from discord_alerts.discord_exit_parser import ExitParser

# Ravish exit keywords — kept in sync with discord_exit_rules.RAVISH_EXIT_KEYWORDS.
# Inlined so the backtest doesn't pull in the bridge/settings import chain.
RAVISH_EXIT_KEYWORDS = [
    "all out", "all-out", "closed", "expired", "closing",
    "take profit", "took profit", "exit", "exiting",
    "100% profit", "100%", "max profit",
]

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
log = logging.getLogger("backtest")

GATEWAY_API   = "https://discord.com/api/v10"
_OPTIONS_MULT = 100
_MSP_ACTIONS  = {"STO", "SELL", "SELL_TO_OPEN"}
_MSP_STRATS   = {"short_put", "short_put_leaps", "cash_secured_put", "msp"}

# ─────────────────────────────────────────────────────────────────────────────
# yfinance price cache  (avoids re-fetching the same ticker repeatedly)
# ─────────────────────────────────────────────────────────────────────────────
_price_cache: dict[tuple, Optional[float]] = {}

def _estimate_put_margin(strike: float, underlying: Optional[float],
                         premium: float, contracts: int) -> float:
    """Reg-T naked-put initial margin — the capital a short put ties up.

    Mirrors llm_roi_analyzer.LLMROIAnalyzer._margin_per_contract (reg_t basis):
        (max(0.20·underlying − OTM, 0.10·strike) + premium) × 100 × contracts

    This is the OFFLINE estimate used when we can't ask Schwab's API for the
    real buying-power reduction.  For an NVDA $200 put it lands ~$3–4k, matching
    the real API figure — not the $250 premium.

    When the underlying price is unknown (yfinance unavailable), the 0.10·strike
    floor still gives a sane notional-based estimate.
    """
    if not strike or strike <= 0:
        # No strike parsed — can't estimate notional; fall back to premium only.
        return premium * _OPTIONS_MULT * contracts
    if underlying and underlying > 0:
        otm = max(underlying - strike, 0.0)          # put is OTM when U > K
        req = max(0.20 * underlying - otm, 0.10 * strike)
    else:
        req = 0.20 * strike                          # notional-based fallback
    return (req + premium) * _OPTIONS_MULT * contracts


def _get_close_price(ticker: str, on_date: date) -> Optional[float]:
    """Return the adjusted close of `ticker` on `on_date` (or nearest prior day)."""
    key = (ticker, on_date)
    if key in _price_cache:
        return _price_cache[key]
    try:
        import yfinance as yf
        start = on_date - timedelta(days=5)
        end   = on_date + timedelta(days=1)
        df    = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                            progress=False, auto_adjust=True)
        if df.empty:
            _price_cache[key] = None
            return None
        price = float(df["Close"].iloc[-1])
        _price_cache[key] = price
        return price
    except Exception as e:
        log.warning("yfinance error for %s on %s: %s", ticker, on_date, e)
        _price_cache[key] = None
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Exit rules loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_rules(rules_path: str) -> dict:
    if not os.path.exists(rules_path):
        return {}
    with open(rules_path) as f:
        data = yaml.safe_load(f)
    return data.get("rules", {})

def _match_rule(rules: dict, action: str, symbol: str, strategy_type: str,
                dte_at_entry: Optional[int]) -> dict:
    """Return the exit rule dict that matches this trade (mirrors ExitRuleEngine._match_rule)."""
    action        = (action or "").upper()
    symbol        = (symbol or "").upper()
    strategy_type = (strategy_type or "").lower()

    for name, rule in rules.items():
        if name == "default":
            continue
        m = rule.get("match", {})
        if "action" in m and m["action"].upper() != action:
            continue
        if "symbol" in m and m["symbol"].upper() != symbol:
            continue
        if "symbols" in m and symbol not in [s.upper() for s in m["symbols"]]:
            continue
        if "strategy_type" in m:
            st = m["strategy_type"]
            if isinstance(st, list):
                if strategy_type not in [s.lower() for s in st]:
                    continue
            elif strategy_type != st.lower():
                continue
        if "min_dte_at_entry" in m:
            if dte_at_entry is None or dte_at_entry < m["min_dte_at_entry"]:
                continue
        return rule

    return rules.get("default", {
        "profit_target_pct": 50,
        "dte_stop": 14,
        "auto_close_at_dte": False,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Discord API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}

def _snowflake_after(dt: datetime) -> str:
    ms = int(dt.timestamp() * 1000) - 1420070400000
    return str(ms << 22)

def _fetch_channel_messages(token: str, channel_id: int, after_dt: datetime,
                            delay: float = 0.4) -> list[dict]:
    """Fetch all messages in channel newer than after_dt, oldest-first."""
    messages = []
    after    = _snowflake_after(after_dt)
    while True:
        url = f"{GATEWAY_API}/channels/{channel_id}/messages?limit=100&after={after}"
        r   = requests.get(url, headers=_headers(token), timeout=15)
        if r.status_code == 429:
            wait = r.json().get("retry_after", 2)
            time.sleep(wait)
            continue
        if r.status_code == 403:
            log.error("No access to channel %s", channel_id)
            return messages
        if r.status_code != 200:
            log.error("HTTP %d fetching channel %s", r.status_code, channel_id)
            break
        batch = r.json()
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        messages.extend(batch)
        after = batch[-1]["id"]
        time.sleep(delay)
    return messages

def _fetch_forum_threads(token: str, forum_id: int, after_dt: datetime,
                         delay: float = 0.4) -> list[dict]:
    """Fetch messages from all active + archived forum threads."""
    all_msgs: list[dict] = []
    after_snow = int(_snowflake_after(after_dt))

    for endpoint in [
        f"{GATEWAY_API}/channels/{forum_id}/threads/search?limit=25&sort_by=last_message_time",
        f"{GATEWAY_API}/channels/{forum_id}/threads/archived/public?limit=100",
    ]:
        r = requests.get(endpoint, headers=_headers(token), timeout=15)
        if r.status_code != 200:
            continue
        threads = r.json().get("threads", [])
        for thread in threads:
            tid = int(thread["id"])
            if tid < after_snow:
                continue
            msgs = _fetch_channel_messages(token, tid, after_dt, delay)
            for m in msgs:
                m["_thread_name"] = thread.get("name", "")
                m["_thread_id"]   = str(tid)
            all_msgs.extend(msgs)
    return all_msgs


# ─────────────────────────────────────────────────────────────────────────────
# Paper account simulation
# ─────────────────────────────────────────────────────────────────────────────

class BacktestAccount:
    def __init__(self, starting_cash: float, msp_multiplier: float) -> None:
        self.cash           = starting_cash
        self.start          = starting_cash
        self.msp_mult       = msp_multiplier
        self.open_positions: list[dict] = []
        self.closed_trades:  list[dict] = []
        self.skipped:        list[dict] = []

    def _is_msp(self, action: str, option_type: str, strategy: str) -> bool:
        return (
            action.upper() in _MSP_ACTIONS
            and option_type.upper().startswith("P")
            and strategy.lower() in _MSP_STRATS
        )

    def open_trade(self, sig, received_at: str) -> bool:
        entry     = sig.paper_entry or sig.limit_price
        contracts = sig.contracts or 1
        action    = (sig.action or "BTO").upper()
        opt_type  = (sig.option_type or "C")
        strategy  = (sig.strategy_type or "unknown")

        if not entry:
            self.skipped.append({"sig": sig, "reason": "no_price", "date": received_at})
            return False

        is_msp = self._is_msp(action, opt_type, strategy)
        strike = sig.strike

        if is_msp:
            # Capital tied up = Reg-T margin (buying-power reduction), NOT premium.
            # A short NVDA $200 put ties up ~$3-4k margin, not the $250 premium.
            underlying = None
            try:
                underlying = _get_close_price(sig.symbol.upper(),
                                              date.fromisoformat(received_at[:10]))
            except Exception:
                underlying = None
            margin = _estimate_put_margin(strike or 0.0, underlying, entry, contracts)
            cost   = margin * self.msp_mult
            method = f"MSP×{self.msp_mult:.1f} (margin≈${margin:,.0f})"
        else:
            # BTO: you pay the premium — that IS the capital out.
            cost   = entry * contracts * _OPTIONS_MULT
            method = "BTO"

        if self.cash < cost:
            self.skipped.append({
                "sig": sig,
                "reason": f"no cash (need ${cost:,.0f} have ${self.cash:,.0f})",
                "date": received_at,
            })
            return False

        self.cash -= cost
        self.open_positions.append({
            "symbol"      : (sig.symbol or "?").upper(),
            "action"      : action,
            "option_type" : opt_type,
            "strategy"    : strategy,
            "contracts"   : contracts,
            "entry"       : entry,
            "cost"        : cost,
            "method"      : method,
            "open_date"   : received_at,
            "expiry"      : sig.expiry_short,
            "strike"      : sig.strike,
            "is_msp"      : is_msp,
            "sig"         : sig,
        })
        return True

    def close_trade(self, symbol: str, exit_price: Optional[float],
                    pnl_pct: Optional[float], reason: str, close_date: str) -> bool:
        symbol = symbol.upper()
        for i in range(len(self.open_positions) - 1, -1, -1):
            pos = self.open_positions[i]
            if pos["symbol"] != symbol:
                continue

            entry  = pos["entry"]
            action = pos["action"]

            # Derive exit price from pnl_pct if not given
            if not exit_price and pnl_pct is not None:
                if action in _MSP_ACTIONS:
                    exit_price = entry * (1 - pnl_pct / 100)
                else:
                    exit_price = entry * (1 + pnl_pct / 100)

            if not exit_price:
                return False

            contracts = pos["contracts"]
            gross_pnl = (exit_price - entry) * contracts * _OPTIONS_MULT
            if action in _MSP_ACTIONS:
                gross_pnl = -gross_pnl   # STO profits when price falls

            self.cash += pos["cost"] + gross_pnl
            self.closed_trades.append({
                **pos,
                "exit"      : exit_price,
                "gross_pnl" : gross_pnl,
                "pnl_pct"   : gross_pnl / pos["cost"] * 100,
                "close_date": close_date,
                "reason"    : reason,
            })
            self.open_positions.pop(i)
            return True
        return False

    @property
    def open_cost(self) -> float:
        return sum(p["cost"] for p in self.open_positions)

    @property
    def equity(self) -> float:
        return self.cash + self.open_cost


# ─────────────────────────────────────────────────────────────────────────────
# Settle open positions that never got an explicit close message
# ─────────────────────────────────────────────────────────────────────────────

def _settle_open_positions(acct: BacktestAccount, rules: dict, today: date,
                           verbose: bool) -> None:
    """
    For each position still open after replaying all messages:

    1. Expired (expiry_short < today):
       - Fetch the underlying's closing price on expiry day from yfinance.
       - SHORT PUT: if stock_close >= strike → expired worthless → 100% profit.
                    if stock_close < strike  → assignment loss → exit at (strike - stock_close).
       - BTO call/put: exit at 0 (expired worthless) — conservative.

    2. DTE-stop hit before expiry (expiry still in future but dte <= dte_stop):
       - Close at rule's profit_target_pct (optimistic) as the rule would have fired.

    3. Truly open (expiry still in future, DTE > dte_stop):
       - Leave as open — cost still tied up, no P&L booked.
    """
    still_open = list(acct.open_positions)  # snapshot — close() mutates the list

    for pos in still_open:
        symbol   = pos["symbol"]
        expiry_s = pos.get("expiry")
        strike   = pos.get("strike")
        action   = pos["action"]
        strategy = pos["strategy"]
        entry    = pos["entry"]
        open_dt  = pos.get("open_date", "")

        if not expiry_s:
            # No expiry info — leave as open
            continue

        try:
            exp_date = date.fromisoformat(expiry_s[:10])
        except ValueError:
            continue

        # ── DTE calculations ───────────────────────────────────────────────
        dte_now = (exp_date - today).days
        try:
            entry_date  = date.fromisoformat(open_dt[:10])
            dte_at_entry = (exp_date - entry_date).days
        except ValueError:
            dte_at_entry = None

        rule = _match_rule(rules, action, symbol, strategy, dte_at_entry)
        dte_stop = rule.get("dte_stop", 14)
        pt_pct   = rule.get("profit_target_pct", 50)

        # ── CASE 1: already expired ────────────────────────────────────────
        if exp_date < today:
            close_date = expiry_s[:10]

            if pos["is_msp"] and strike:
                stock_price = _get_close_price(symbol, exp_date)
                if stock_price is None:
                    # Can't get price — assume 50% PT (conservative)
                    reason    = "expired_no_data"
                    exit_price = entry * (1 - pt_pct / 100)
                elif stock_price >= strike:
                    # Expired OTM → worthless → 100% profit (keep full premium)
                    reason     = "expired_worthless"
                    exit_price = 0.0
                else:
                    # Expired ITM → put holder exercises → loss = strike - stock_price
                    intrinsic  = strike - stock_price
                    exit_price = intrinsic   # our short put is now worth intrinsic
                    reason     = f"expired_itm (stock={stock_price:.2f} strike={strike:.2f})"
            else:
                # BTO option expired → worthless
                reason     = "expired_worthless"
                exit_price = 0.0

            closed = acct.close_trade(symbol, exit_price, None, reason, close_date)
            if closed and verbose:
                trade = acct.closed_trades[-1]
                print(f"{close_date}  {'EXPRY':6}  {'':4} {symbol:8} "
                      f"{'':18} {'':4}  {exit_price:7.2f}  {trade['gross_pnl']:+9,.2f}  "
                      f"{acct.cash:12,.2f}  {reason}")
            continue

        # ── CASE 2: DTE-stop rule would have fired already ────────────────
        if dte_now <= dte_stop and rule.get("auto_close_at_dte", True):
            # Rule would have closed this when DTE hit dte_stop.
            # Use profit_target_pct as the assumed exit (conservative).
            close_date = (today - timedelta(days=dte_now - dte_stop)).isoformat()
            if pos["is_msp"]:
                exit_price = entry * (1 - pt_pct / 100)
            else:
                exit_price = entry * (1 + pt_pct / 100)
            reason = f"dte_stop({dte_stop}) assumed {pt_pct:.0f}%pt"
            closed = acct.close_trade(symbol, exit_price, None, reason, close_date)
            if closed and verbose:
                trade = acct.closed_trades[-1]
                print(f"{close_date}  {'DTE':6}  {'':4} {symbol:8} "
                      f"{'':18} {'':4}  {exit_price:7.2f}  {trade['gross_pnl']:+9,.2f}  "
                      f"{acct.cash:12,.2f}  {reason}")
            continue

        # ── CASE 3: genuinely still open ──────────────────────────────────
        # Leave in acct.open_positions — will show in summary


# ─────────────────────────────────────────────────────────────────────────────
# Main backtest
# ─────────────────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "status",          # closed | open
    "symbol",
    "action",          # STO | BTO
    "strategy",
    "option_type",     # PUT | CALL
    "strike",
    "expiry",
    "contracts",
    "entry_date",
    "entry_price",     # premium per share
    "capital_tied",    # $ locked up (MSP margin×mult, or BTO premium×100)
    "margin_method",   # MSP×2.0 (margin≈$X) | BTO
    "exit_date",
    "exit_price",      # premium per share at close (0 = expired worthless)
    "gross_pnl",       # $ realised P&L
    "return_pct",      # gross_pnl / capital_tied × 100
    "exit_reason",
    "hold_days",
]


def _hold_days(open_d: Optional[str], close_d: Optional[str]) -> Optional[int]:
    if not open_d or not close_d:
        return None
    try:
        return (date.fromisoformat(close_d[:10]) - date.fromisoformat(open_d[:10])).days
    except ValueError:
        return None


def _write_trades_csv(acct: "BacktestAccount", path: str) -> None:
    """Write one row per trade (closed + still-open) for later analysis."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        w.writeheader()

        for t in acct.closed_trades:
            w.writerow({
                "status"       : "closed",
                "symbol"       : t.get("symbol"),
                "action"       : t.get("action"),
                "strategy"     : t.get("strategy"),
                "option_type"  : t.get("option_type"),
                "strike"       : t.get("strike"),
                "expiry"       : t.get("expiry"),
                "contracts"    : t.get("contracts"),
                "entry_date"   : t.get("open_date"),
                "entry_price"  : round(t.get("entry", 0) or 0, 4),
                "capital_tied" : round(t.get("cost", 0) or 0, 2),
                "margin_method": t.get("method"),
                "exit_date"    : t.get("close_date"),
                "exit_price"   : round(t.get("exit", 0) or 0, 4),
                "gross_pnl"    : round(t.get("gross_pnl", 0) or 0, 2),
                "return_pct"   : round(t.get("pnl_pct", 0) or 0, 2),
                "exit_reason"  : t.get("reason"),
                "hold_days"    : _hold_days(t.get("open_date"), t.get("close_date")),
            })

        for p in acct.open_positions:
            w.writerow({
                "status"       : "open",
                "symbol"       : p.get("symbol"),
                "action"       : p.get("action"),
                "strategy"     : p.get("strategy"),
                "option_type"  : p.get("option_type"),
                "strike"       : p.get("strike"),
                "expiry"       : p.get("expiry"),
                "contracts"    : p.get("contracts"),
                "entry_date"   : p.get("open_date"),
                "entry_price"  : round(p.get("entry", 0) or 0, 4),
                "capital_tied" : round(p.get("cost", 0) or 0, 2),
                "margin_method": p.get("method"),
                "exit_date"    : "",
                "exit_price"   : "",
                "gross_pnl"    : "",
                "return_pct"   : "",
                "exit_reason"  : "still_open",
                "hold_days"    : "",
            })


def _msg_dt(msg: dict) -> datetime:
    ts = msg.get("timestamp", "")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def run_backtest(token, signal_channel_id, update_channel_id, forum_channel_id,
                 from_date, starting_balance, msp_multiplier, verbose,
                 csv_path=None) -> None:
    after_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    today    = date.today()

    rules_path = os.path.join(os.path.dirname(__file__), "exit_rules.yaml")
    rules      = _load_rules(rules_path)

    print(f"\n{'='*72}")
    print(f"  Discord Backtest  —  Ravish alerts replay")
    print(f"  From:      {from_date}")
    print(f"  Balance:   ${starting_balance:,.2f}")
    print(f"  MSP mult:  {msp_multiplier:.1f}×")
    print(f"  Today:     {today}")
    print(f"{'='*72}")

    # ── Fetch messages ────────────────────────────────────────────────────
    print("\nFetching signal channel messages...")
    signal_msgs = _fetch_channel_messages(token, signal_channel_id, after_dt)
    print(f"  → {len(signal_msgs)} messages")

    print("Fetching update channel messages...")
    update_msgs = _fetch_channel_messages(token, update_channel_id, after_dt)
    print(f"  → {len(update_msgs)} messages")

    print("Fetching forum thread messages...")
    forum_msgs = _fetch_forum_threads(token, forum_channel_id, after_dt)
    print(f"  → {len(forum_msgs)} messages")

    all_msgs = []
    for m in signal_msgs:  m["_source"] = "signal"
    for m in update_msgs:  m["_source"] = "update"
    for m in forum_msgs:   m["_source"] = "forum"
    all_msgs = signal_msgs + update_msgs + forum_msgs
    all_msgs.sort(key=_msg_dt)
    print(f"\nTotal messages: {len(all_msgs)}\n")

    parser      = AlertParser()
    exit_parser = ExitParser()
    acct        = BacktestAccount(starting_balance, msp_multiplier)

    parsed_entries = 0
    ravish_exits   = 0

    if verbose:
        print(f"{'Date':10}  {'Src':6}  {'Act':4} {'Sym':8} {'Strat':18} "
              f"{'Ctrs':4}  {'Price':>7}  {'Cost':>9}  {'Cash':>12}  Status")
        print("─" * 102)

    for msg in all_msgs:
        source  = msg["_source"]
        content = msg.get("content", "") or ""
        if not content.strip():
            continue

        author     = (msg.get("author") or {}).get("username", "?")
        msg_dt     = _msg_dt(msg)
        date_str   = msg_dt.strftime("%Y-%m-%d")
        channel_id = str(msg.get("channel_id", msg.get("_thread_id", "")))

        # ── Entry signals ─────────────────────────────────────────────────
        if source in ("signal", "forum"):
            sig = parser.parse(text=content, message_id=msg["id"],
                               channel_id=channel_id, author=author)
            if sig and sig.is_tradeable:
                parsed_entries += 1
                opened = acct.open_trade(sig, date_str)
                if verbose:
                    entry = sig.paper_entry or sig.limit_price or 0
                    if opened:
                        pos    = acct.open_positions[-1]
                        cost   = pos["cost"]
                        method = pos["method"]
                        status = f"OPEN  {method}"
                    else:
                        cost   = 0.0
                        status = "SKIP"
                    print(f"{date_str}  {source:6}  {sig.action or '?':4} "
                          f"{sig.symbol or '?':8} {sig.strategy_type or '?':18} "
                          f"{sig.contracts or 1:4}  {entry:7.2f}  {cost:9,.2f}  "
                          f"{acct.cash:12,.2f}  {status}")

        # ── Exit signals ──────────────────────────────────────────────────
        if source in ("update", "forum"):
            lower     = content.lower()
            ravish_kw = next((k for k in RAVISH_EXIT_KEYWORDS if k in lower), None)
            exit_sig  = exit_parser.parse(content)
            pnl_pct   = getattr(exit_sig, "pnl_pct",   None) if exit_sig else None
            exit_price = getattr(exit_sig, "exit_price", None) if exit_sig else None
            symbol     = getattr(exit_sig, "symbol",    None) if exit_sig else None

            # Fall back to thread name for symbol
            if not symbol:
                thread_name = msg.get("_thread_name", "")
                if thread_name:
                    symbol = thread_name.split()[0].upper().strip("$")

            if ravish_kw and symbol:
                closed = acct.close_trade(symbol, exit_price, pnl_pct, ravish_kw, date_str)
                if closed:
                    ravish_exits += 1
                    if verbose:
                        trade = acct.closed_trades[-1]
                        ep = trade.get("exit", 0) or 0
                        print(f"{date_str}  {source:6}  {'EXIT':4} "
                              f"{symbol:8} {'':18} {'':4}  {ep:7.2f}  "
                              f"{trade['gross_pnl']:+9,.2f}  {acct.cash:12,.2f}  "
                              f"CLOSED ({trade['pnl_pct']:+.1f}%)")

            elif exit_sig and symbol and pnl_pct and pnl_pct >= 50:
                # Profit target hit in update message (no explicit ravish keyword)
                closed = acct.close_trade(symbol, exit_price, pnl_pct, "profit_target", date_str)
                if closed and verbose:
                    trade = acct.closed_trades[-1]
                    ep = trade.get("exit", 0) or 0
                    print(f"{date_str}  {source:6}  {'→PT':4} "
                          f"{symbol:8} {'':18} {'':4}  {ep:7.2f}  "
                          f"{trade['gross_pnl']:+9,.2f}  {acct.cash:12,.2f}  "
                          f"PT ({pnl_pct:.0f}%)")

    # ── Settle positions that never got an explicit close ─────────────────
    print("\nSettling open positions (expiry / DTE-stop / yfinance)...")
    _settle_open_positions(acct, rules, today, verbose)

    # ── Monthly breakdown ─────────────────────────────────────────────────
    monthly: dict[str, dict] = {}
    for t in acct.closed_trades:
        month = (t["open_date"] or "")[:7]
        if month not in monthly:
            monthly[month] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        monthly[month]["trades"] += 1
        pnl = t["gross_pnl"]
        monthly[month]["pnl"] += pnl
        if pnl > 0:
            monthly[month]["wins"]   += 1
        else:
            monthly[month]["losses"] += 1

    total_pnl    = sum(t["gross_pnl"] for t in acct.closed_trades)
    wins         = sum(1 for t in acct.closed_trades if t["gross_pnl"] > 0)
    losses       = sum(1 for t in acct.closed_trades if t["gross_pnl"] <= 0)
    win_rate     = wins / len(acct.closed_trades) * 100 if acct.closed_trades else 0
    total_return = (acct.equity - starting_balance) / starting_balance * 100

    print(f"\n{'─'*72}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"{'─'*72}")
    print(f"  {'Month':<10}  {'Trades':>6}  {'Wins':>5}  {'Loss':>5}  {'Win%':>6}  {'P&L':>12}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*12}")
    for month in sorted(monthly):
        m  = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] else 0
        print(f"  {month:<10}  {m['trades']:>6}  {m['wins']:>5}  {m['losses']:>5}  "
              f"{wr:>5.0f}%  {m['pnl']:>+12,.2f}")

    print(f"\n{'─'*72}")
    print(f"  RESULTS")
    print(f"{'─'*72}")
    print(f"  Messages processed  :  {len(all_msgs)}")
    print(f"  Entry signals found :  {parsed_entries}")
    print(f"  Ravish exits caught :  {ravish_exits}")
    print(f"  Closed trades       :  {len(acct.closed_trades)}  "
          f"(W={wins}  L={losses}  wr={win_rate:.0f}%)")
    print(f"  Still open          :  {len(acct.open_positions)}  "
          f"(cost tied up: ${acct.open_cost:,.2f})")
    print(f"  Skipped             :  {len(acct.skipped)}")
    print()
    print(f"  Starting balance    :  ${starting_balance:>12,.2f}")
    print(f"  Realised P&L        :  ${total_pnl:>+12,.2f}")
    print(f"  Cash on hand        :  ${acct.cash:>12,.2f}")
    print(f"  Open book cost      :  ${acct.open_cost:>12,.2f}")
    print(f"  Equity (cash+open)  :  ${acct.equity:>12,.2f}  ({total_return:+.2f}%)")
    print(f"{'─'*72}\n")

    # ── Write per-trade CSV for later analysis ─────────────────────────────
    if csv_path:
        _write_trades_csv(acct, csv_path)
        n = len(acct.closed_trades) + len(acct.open_positions)
        print(f"  Wrote {n} trade rows → {csv_path}\n")

    # ── Still-open positions ───────────────────────────────────────────────
    if acct.open_positions:
        print(f"  STILL OPEN ({len(acct.open_positions)}) — no exit found, expiry still in future")
        print(f"  {'Open':10}  {'Act':4} {'Sym':8} {'Strat':18} "
              f"{'Ctrs':4}  {'Entry':>7}  {'Strike':>7}  {'Expiry':>12}  {'Cost':>9}")
        for p in acct.open_positions:
            print(f"  {p['open_date']:10}  {p['action'] or '?':4} {p['symbol']:8} "
                  f"{p['strategy'] or '?':18} {p['contracts']:4}  {p['entry']:7.2f}  "
                  f"{p.get('strike') or 0:7.2f}  {p.get('expiry') or '?':>12}  "
                  f"{p['cost']:9,.2f}")
        print()

    # ── Skipped ───────────────────────────────────────────────────────────
    if acct.skipped:
        print(f"  SKIPPED ({len(acct.skipped)})")
        for s in acct.skipped[:8]:
            sig = s["sig"]
            print(f"  {s['date']}  {sig.action or '?'} {sig.symbol or '?'}  → {s['reason']}")
        if len(acct.skipped) > 8:
            print(f"  ... and {len(acct.skipped)-8} more")
        print()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Replay Ravish Discord alerts through paper account")
    p.add_argument("--from",       dest="from_date",  default="2026-09-01",
                   help="Start date YYYY-MM-DD  (default: 2026-09-01)")
    p.add_argument("--balance",    type=float, default=100_000.0,
                   help="Starting balance        (default: 100000)")
    p.add_argument("--multiplier", type=float, default=2.0,
                   help="MSP margin multiplier   (default: 2.0)")
    p.add_argument("--verbose",    action="store_true",
                   help="Print each trade as processed")
    p.add_argument("--csv",        dest="csv_path", default=None,
                   help="Write per-trade CSV to this path "
                        "(default: paper_trading/trades/discord_backtest_<from>.csv; "
                        "pass 'none' to skip)")
    args = p.parse_args()

    # Load token from .env if not in environment
    token = os.getenv("DISCORD_USER_TOKEN")
    if not token:
        env_file = os.path.join(_root, ".env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DISCORD_USER_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

    if not token:
        print("ERROR: DISCORD_USER_TOKEN not set in .env")
        sys.exit(1)

    signal_channel_id = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID", "1418852039382925353"))
    update_channel_id = int(os.getenv("DISCORD_UPDATE_CHANNEL_ID", "1466838176202231829"))
    forum_channel_id  = int(os.getenv("DISCORD_FORUM_CHANNEL_ID",  "1427087208657059952"))

    # Resolve CSV path: explicit --csv, 'none' to skip, else a dated default.
    if args.csv_path is None:
        csv_path = os.path.join(_root, "paper_trading", "trades",
                                f"discord_backtest_{args.from_date}.csv")
    elif args.csv_path.lower() == "none":
        csv_path = None
    else:
        csv_path = args.csv_path

    run_backtest(
        token             = token,
        signal_channel_id = signal_channel_id,
        update_channel_id = update_channel_id,
        forum_channel_id  = forum_channel_id,
        from_date         = args.from_date,
        starting_balance  = args.balance,
        msp_multiplier    = args.multiplier,
        verbose           = args.verbose,
        csv_path          = csv_path,
    )


if __name__ == "__main__":
    main()
