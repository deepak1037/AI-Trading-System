"""discord_alerts/discord_backtest.py

Replay real historical Ravish Discord alerts through the paper account
simulator — as if the listener had been running since a given date.

What it does:
  1. Fetches ALL messages from the signal channel + income-trades forum
     back to --from date via Discord REST API (paginated with ?before=).
  2. Runs each message through the same DiscordSignalParser used by the
     live listener — no code duplication.
  3. Runs exit-keyword detection on update/forum messages.
  4. Simulates the $100k paper account with the same MSP 2× buffer rule.
  5. Prints a per-trade table + monthly breakdown + final P&L summary.

No money moves, no DB writes, no Schwab calls.

Usage:
    python discord_alerts/discord_backtest.py
    python discord_alerts/discord_backtest.py --from 2026-09-01
    python discord_alerts/discord_backtest.py --from 2026-08-01 --balance 100000
    python discord_alerts/discord_backtest.py --from 2026-09-01 --multiplier 1.5 --verbose

Requires:
    DISCORD_USER_TOKEN         in .env (same token the live listener uses)
    DISCORD_SIGNAL_CHANNEL_ID  (default 1418852039382925353)
    DISCORD_UPDATE_CHANNEL_ID  (default 1466838176202231829)
    DISCORD_FORUM_CHANNEL_ID   (default 1427087208657059952)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ── project root on path ──────────────────────────────────────────────────────
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from config.settings import settings
from discord_alerts.discord_signal_reader import DiscordSignalParser, ParsedSignal
from discord_alerts.discord_exit_parser import ExitParser
from discord_alerts.discord_exit_rules import RAVISH_EXIT_KEYWORDS

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("backtest")

# ── constants ─────────────────────────────────────────────────────────────────
GATEWAY_API      = "https://discord.com/api/v10"
_OPTIONS_MULT    = 100
_MSP_ACTIONS     = {"STO", "SELL", "SELL_TO_OPEN"}
_MSP_STRATEGIES  = {"short_put", "short_put_leaps", "cash_secured_put", "msp"}


# ─────────────────────────────────────────────────────────────────────────────
# Discord API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json"}


def _snowflake_from_date(dt: datetime) -> int:
    """Convert a UTC datetime to the Discord snowflake ID that started that second."""
    # Discord epoch: 2015-01-01T00:00:00Z = 1420070400000 ms
    ms = int(dt.timestamp() * 1000) - 1420070400000
    return (ms << 22)


def _fetch_channel_messages(
    token: str,
    channel_id: int,
    after_dt: datetime,
    delay: float = 0.5,
) -> list[dict]:
    """Fetch all messages in channel_id newer than after_dt, oldest-first."""
    after_snowflake = _snowflake_from_date(after_dt)
    messages = []
    after = str(after_snowflake)

    while True:
        url = f"{GATEWAY_API}/channels/{channel_id}/messages?limit=100&after={after}"
        r   = requests.get(url, headers=_headers(token), timeout=15)

        if r.status_code == 429:
            wait = r.json().get("retry_after", 2)
            log.warning("Rate limited — sleeping %.1fs", wait)
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

        # Discord returns newest-first when using ?after — reverse to oldest-first
        batch.sort(key=lambda m: int(m["id"]))
        messages.extend(batch)
        after = batch[-1]["id"]   # continue from the last (newest) id we got
        log.info("  fetched %d msgs (total %d)", len(batch), len(messages))
        time.sleep(delay)

    return messages


def _fetch_forum_threads(
    token: str,
    forum_channel_id: int,
    after_dt: datetime,
    delay: float = 0.5,
) -> list[dict]:
    """Fetch messages from all forum threads newer than after_dt."""
    all_msgs: list[dict] = []

    # Get active threads
    url = f"{GATEWAY_API}/channels/{forum_channel_id}/threads/search?limit=25&sort_by=last_message_time"
    r   = requests.get(url, headers=_headers(token), timeout=15)
    if r.status_code != 200:
        log.warning("Cannot fetch forum threads: HTTP %d", r.status_code)
        return []

    data    = r.json()
    threads = data.get("threads", [])

    # Also get archived threads
    arch_url = f"{GATEWAY_API}/channels/{forum_channel_id}/threads/archived/public?limit=100"
    r2 = requests.get(arch_url, headers=_headers(token), timeout=15)
    if r2.status_code == 200:
        threads += r2.json().get("threads", [])

    log.info("Forum: found %d threads", len(threads))

    after_snowflake = _snowflake_from_date(after_dt)

    for thread in threads:
        tid = int(thread["id"])
        # Skip threads created before our window
        if tid < after_snowflake:
            continue

        thread_msgs = _fetch_channel_messages(token, tid, after_dt, delay)
        # Tag each message with thread metadata
        for m in thread_msgs:
            m["_thread_name"] = thread.get("name", "")
            m["_thread_id"]   = str(tid)
        all_msgs.extend(thread_msgs)

    return all_msgs


# ─────────────────────────────────────────────────────────────────────────────
# Paper account simulation
# ─────────────────────────────────────────────────────────────────────────────

class BacktestAccount:
    def __init__(self, starting_cash: float, msp_multiplier: float) -> None:
        self.cash            = starting_cash
        self.start           = starting_cash
        self.msp_multiplier  = msp_multiplier
        self.open_positions: list[dict]   = []
        self.closed_trades:  list[dict]   = []
        self.skipped:        list[dict]   = []

    def _is_msp(self, sig: ParsedSignal) -> bool:
        return (
            (sig.action or "").upper() in _MSP_ACTIONS
            and (sig.option_type or "").upper().startswith("P")
            and (sig.strategy_type or "").lower() in _MSP_STRATEGIES
        )

    def open_trade(self, sig: ParsedSignal, received_at: str) -> bool:
        entry = sig.paper_entry or sig.limit_price
        if not entry:
            self.skipped.append({"sig": sig, "reason": "no_price", "date": received_at})
            return False

        contracts = sig.contracts or 1
        base_cost = entry * contracts * _OPTIONS_MULT

        if self._is_msp(sig):
            cost   = base_cost * self.msp_multiplier
            method = f"MSP×{self.msp_multiplier:.1f}"
        else:
            cost   = base_cost
            method = "BTO"

        if self.cash < cost:
            self.skipped.append({
                "sig": sig, "reason": f"insufficient_cash (need ${cost:,.0f} have ${self.cash:,.0f})",
                "date": received_at,
            })
            return False

        self.cash -= cost
        self.open_positions.append({
            "symbol"   : sig.symbol,
            "action"   : sig.action,
            "strategy" : sig.strategy_type,
            "contracts": contracts,
            "entry"    : entry,
            "cost"     : cost,
            "method"   : method,
            "open_date": received_at,
            "sig"      : sig,
        })
        return True

    def close_trade(
        self,
        symbol: str,
        exit_price: Optional[float],
        pnl_pct: Optional[float],
        reason: str,
        close_date: str,
    ) -> bool:
        """Close the most-recent open position for symbol."""
        # Find matching open position (LIFO — most recent first, like live system)
        for i in range(len(self.open_positions) - 1, -1, -1):
            pos = self.open_positions[i]
            if pos["symbol"].upper() != symbol.upper():
                continue

            # Derive exit price from pnl_pct if needed
            if not exit_price and pnl_pct is not None:
                entry  = pos["entry"]
                action = (pos["action"] or "BTO").upper()
                if action in _MSP_ACTIONS:
                    exit_price = entry * (1 - pnl_pct / 100)
                else:
                    exit_price = entry * (1 + pnl_pct / 100)

            if not exit_price:
                return False

            entry     = pos["entry"]
            contracts = pos["contracts"]
            action    = (pos["action"] or "BTO").upper()

            gross_pnl = (exit_price - entry) * contracts * _OPTIONS_MULT
            if action in _MSP_ACTIONS:
                gross_pnl = -gross_pnl

            # Return cost + P&L
            self.cash += pos["cost"] + gross_pnl

            closed = {
                **pos,
                "exit"       : exit_price,
                "gross_pnl"  : gross_pnl,
                "pnl_pct"    : gross_pnl / pos["cost"] * 100,
                "close_date" : close_date,
                "reason"     : reason,
            }
            self.closed_trades.append(closed)
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
# Main backtest runner
# ─────────────────────────────────────────────────────────────────────────────

def _msg_dt(msg: dict) -> datetime:
    """Parse Discord message timestamp to UTC datetime."""
    ts = msg.get("timestamp", "")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def run_backtest(
    token: str,
    signal_channel_id: int,
    update_channel_id: int,
    forum_channel_id: int,
    from_date: str,
    starting_balance: float,
    msp_multiplier: float,
    verbose: bool,
) -> None:
    after_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)

    print(f"\n{'='*70}")
    print(f"  Discord Backtest  —  Ravish alerts replay")
    print(f"  From:      {from_date}")
    print(f"  Balance:   ${starting_balance:,.2f}")
    print(f"  MSP mult:  {msp_multiplier:.1f}×")
    print(f"{'='*70}")

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

    # ── Merge and sort by timestamp ───────────────────────────────────────
    all_msgs = []
    for m in signal_msgs:
        m["_source"] = "signal"
        all_msgs.append(m)
    for m in update_msgs:
        m["_source"] = "update"
        all_msgs.append(m)
    for m in forum_msgs:
        m["_source"] = "forum"
        all_msgs.append(m)

    all_msgs.sort(key=_msg_dt)
    print(f"\nTotal messages to process: {len(all_msgs)}")

    # ── Set up parsers and account ────────────────────────────────────────
    parser      = DiscordSignalParser()
    exit_parser = ExitParser()
    acct        = BacktestAccount(starting_balance, msp_multiplier)

    parsed_entries = 0
    ravish_exits   = 0

    if verbose:
        print(f"\n{'Date':10}  {'Src':6}  {'Act':4} {'Sym':8} {'Strat':18} {'Ctrs':4}  {'Entry':>7}  {'Cost':>9}  {'Cash':>12}  Status")
        print("-" * 100)

    for msg in all_msgs:
        source  = msg["_source"]
        content = msg.get("content", "") or ""
        if not content.strip():
            continue

        author     = (msg.get("author") or {}).get("username", "?")
        msg_dt     = _msg_dt(msg)
        date_str   = msg_dt.strftime("%Y-%m-%d")
        channel_id = str(msg.get("channel_id", msg.get("_thread_id", "")))

        # ── Entry signal (signal channel + forum first message) ───────────
        if source in ("signal", "forum"):
            sig = parser.parse(
                text       = content,
                message_id = msg["id"],
                channel_id = channel_id,
                author     = author,
            )
            if sig and sig.is_tradeable:
                parsed_entries += 1
                opened = acct.open_trade(sig, date_str)
                if verbose:
                    status = "OPEN" if opened else "SKIP"
                    entry  = sig.paper_entry or sig.limit_price or 0
                    cost   = (entry * (sig.contracts or 1) * _OPTIONS_MULT *
                              (msp_multiplier if acct._is_msp(sig) else 1))
                    print(f"{date_str}  {source:6}  {sig.action or '?':4} "
                          f"{sig.symbol or '?':8} {sig.strategy_type or '?':18} "
                          f"{sig.contracts or 1:4}  {entry:7.2f}  {cost:9,.2f}  "
                          f"{acct.cash:12,.2f}  {status}")

        # ── Exit signal (update channel + forum subsequent messages) ───────
        if source in ("update", "forum"):
            # Check Ravish exit keywords first
            lower = content.lower()
            ravish_kw = next((k for k in RAVISH_EXIT_KEYWORDS if k in lower), None)

            if ravish_kw:
                # Try to identify which symbol
                exit_sig = exit_parser.parse(content)
                symbol   = getattr(exit_sig, "symbol", None)

                if symbol:
                    pnl_pct    = getattr(exit_sig, "pnl_pct", None)
                    exit_price = getattr(exit_sig, "exit_price", None)
                    closed = acct.close_trade(symbol, exit_price, pnl_pct, ravish_kw, date_str)
                    if closed:
                        ravish_exits += 1
                        if verbose:
                            trade = acct.closed_trades[-1]
                            print(f"{date_str}  {source:6}  {'EXIT':4} "
                                  f"{symbol:8} {'':18} {'':4}  "
                                  f"{trade['exit']:7.2f}  {trade['gross_pnl']:+9,.2f}  "
                                  f"{acct.cash:12,.2f}  CLOSED ({trade['pnl_pct']:+.1f}%)")
                else:
                    # Ravish exit but no symbol parsed — try to close any open position
                    # by looking at context (the forum thread's symbol)
                    thread_name = msg.get("_thread_name", "")
                    if thread_name:
                        # Thread name typically starts with the ticker
                        sym = thread_name.split()[0].upper().strip("$")
                        if sym:
                            exit_sig2  = exit_parser.parse(content)
                            pnl_pct    = getattr(exit_sig2, "pnl_pct", None)
                            exit_price = getattr(exit_sig2, "exit_price", None)
                            closed = acct.close_trade(sym, exit_price, pnl_pct, ravish_kw, date_str)
                            if closed:
                                ravish_exits += 1
                                if verbose:
                                    trade = acct.closed_trades[-1]
                                    print(f"{date_str}  {source:6}  {'EXIT':4} "
                                          f"{sym:8} {'(thread)':18} {'':4}  "
                                          f"{trade.get('exit', 0):7.2f}  {trade['gross_pnl']:+9,.2f}  "
                                          f"{acct.cash:12,.2f}  CLOSED ({trade['pnl_pct']:+.1f}%)")

            else:
                # Non-ravish update: try exit parser for profit % updates
                exit_sig = exit_parser.parse(content)
                if exit_sig and exit_sig.symbol and exit_sig.pnl_pct:
                    # Only auto-close if pnl_pct hits the profit target (50%+)
                    if (exit_sig.pnl_pct or 0) >= 50:
                        closed = acct.close_trade(
                            exit_sig.symbol, exit_sig.exit_price,
                            exit_sig.pnl_pct, "profit_target", date_str,
                        )
                        if closed and verbose:
                            trade = acct.closed_trades[-1]
                            print(f"{date_str}  {source:6}  {'→PT':4} "
                                  f"{exit_sig.symbol:8} {'':18} {'':4}  "
                                  f"{trade.get('exit', 0):7.2f}  {trade['gross_pnl']:+9,.2f}  "
                                  f"{acct.cash:12,.2f}  PT ({exit_sig.pnl_pct:.0f}%)")

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
            monthly[month]["wins"] += 1
        else:
            monthly[month]["losses"] += 1

    total_pnl    = sum(t["gross_pnl"] for t in acct.closed_trades)
    wins         = sum(1 for t in acct.closed_trades if t["gross_pnl"] > 0)
    losses       = sum(1 for t in acct.closed_trades if t["gross_pnl"] <= 0)
    win_rate     = wins / len(acct.closed_trades) * 100 if acct.closed_trades else 0
    total_return = (acct.equity - starting_balance) / starting_balance * 100

    print(f"\n{'─'*70}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"{'─'*70}")
    print(f"  {'Month':<10}  {'Trades':>6}  {'Wins':>5}  {'Loss':>5}  {'Win%':>6}  {'P&L':>12}")
    print(f"  {'─'*10}  {'─'*6}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*12}")
    for month in sorted(monthly):
        m  = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] else 0
        print(f"  {month:<10}  {m['trades']:>6}  {m['wins']:>5}  {m['losses']:>5}  {wr:>5.0f}%  {m['pnl']:>+12,.2f}")

    print(f"\n{'─'*70}")
    print(f"  RESULTS")
    print(f"{'─'*70}")
    print(f"  Messages processed  :  {len(all_msgs)}")
    print(f"  Entry signals parsed:  {parsed_entries}")
    print(f"  Ravish exits found  :  {ravish_exits}")
    print(f"  Closed trades       :  {len(acct.closed_trades)}  (W={wins} L={losses} wr={win_rate:.0f}%)")
    print(f"  Open positions      :  {len(acct.open_positions)}  (tied up: ${acct.open_cost:,.2f})")
    print(f"  Skipped             :  {len(acct.skipped)}")
    print(f"")
    print(f"  Starting balance    :  ${starting_balance:>12,.2f}")
    print(f"  Realised P&L        :  ${total_pnl:>+12,.2f}")
    print(f"  Cash                :  ${acct.cash:>12,.2f}")
    print(f"  Open book cost      :  ${acct.open_cost:>12,.2f}")
    print(f"  Equity (cash+open)  :  ${acct.equity:>12,.2f}  ({total_return:+.2f}%)")
    print(f"{'─'*70}\n")

    if acct.open_positions:
        print(f"  OPEN POSITIONS")
        print(f"  {'Open':10}  {'Act':4} {'Sym':8} {'Strat':18} {'Ctrs':4}  {'Entry':>7}  {'Cost':>9}  Method")
        for p in acct.open_positions:
            print(f"  {p['open_date']:10}  {p['action'] or '?':4} {p['symbol'] or '?':8} "
                  f"{p['strategy'] or '?':18} {p['contracts']:4}  {p['entry']:7.2f}  "
                  f"{p['cost']:9,.2f}  {p['method']}")
        print()

    if acct.skipped:
        print(f"  SKIPPED TRADES ({len(acct.skipped)})")
        for s in acct.skipped[:10]:
            sig = s["sig"]
            print(f"  {s['date']}  {sig.action or '?'} {sig.symbol or '?'}  → {s['reason']}")
        if len(acct.skipped) > 10:
            print(f"  ... and {len(acct.skipped)-10} more")
        print()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Replay Ravish Discord alerts through paper account")
    p.add_argument("--from",       dest="from_date",  default="2026-09-01",
                   help="Start date YYYY-MM-DD (default: 2026-09-01)")
    p.add_argument("--balance",    type=float, default=100_000.0,
                   help="Starting balance (default: 100000)")
    p.add_argument("--multiplier", type=float, default=2.0,
                   help="MSP margin multiplier (default: 2.0)")
    p.add_argument("--verbose",    action="store_true",
                   help="Print each trade as it is processed")
    args = p.parse_args()

    token = os.getenv("DISCORD_USER_TOKEN")
    if not token:
        # Try loading .env manually
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

    signal_channel_id = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID",  "1418852039382925353"))
    update_channel_id = int(os.getenv("DISCORD_UPDATE_CHANNEL_ID",  "1466838176202231829"))
    forum_channel_id  = int(os.getenv("DISCORD_FORUM_CHANNEL_ID",   "1427087208657059952"))

    run_backtest(
        token             = token,
        signal_channel_id = signal_channel_id,
        update_channel_id = update_channel_id,
        forum_channel_id  = forum_channel_id,
        from_date         = args.from_date,
        starting_balance  = args.balance,
        msp_multiplier    = args.multiplier,
        verbose           = args.verbose,
    )


if __name__ == "__main__":
    main()
