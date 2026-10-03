"""
discord_alerts/discord_user_listener.py

Real-time Discord listener using user token.
Watches THREE channels simultaneously:
  - SIGNAL channel      -> parse entry alerts -> save as open paper trades
  - UPDATE channel      -> parse profit updates -> evaluate exit rules
  - INCOME-TRADES forum -> parse thread entries + updates -> primary source

Add to .env:
    DISCORD_USER_TOKEN=your_token
    DISCORD_SIGNAL_CHANNEL_ID=1418852039382925353
    DISCORD_UPDATE_CHANNEL_ID=1466838176202231829
    DISCORD_FORUM_CHANNEL_ID=1427087208657059952
    DISCORD_OWNER_ID=1424445514212049118
    DB_PATH=db/trading.db
    DRY_RUN=true
    EXIT_RULES_PATH=discord_alerts/exit_rules.yaml

Run:
    python -m discord_alerts.discord_user_listener
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

GATEWAY_URL      = "wss://gateway.discord.gg/?v=10&encoding=json"
GATEWAY_API      = "https://discord.com/api/v10"
OP_DISPATCH      = 0
OP_HEARTBEAT     = 1
OP_IDENTIFY      = 2
OP_HEARTBEAT_ACK = 11


def _extract_text(msg: dict) -> str:
    text = msg.get("content", "") or ""
    for embed in msg.get("embeds", []):
        desc = embed.get("description") or ""
        if desc:
            text += "\n" + desc
        for field in embed.get("fields", []):
            text += f"\n{field.get('name','')} {field.get('value','')}"
    return text.strip()


def make_entry_handler(parser, db, rule_engine, dry_run: bool, bridge=None):
    async def handle(msg: dict):
        text = _extract_text(msg)
        if not text:
            return
        message_id = str(msg.get("id", ""))
        author     = msg.get("author", {}).get("username", "unknown")
        channel_id = str(msg.get("channel_id", ""))
        sig = parser.parse(text=text, message_id=message_id,
                           channel_id=channel_id, author=author)
        if sig is None:
            return
        saved = db.save(sig)
        if not saved:
            return
        if sig.is_tradeable:
            log.info("ENTRY [%s] %s  confidence=%s  entry=$%s",
                     "DRY" if dry_run else "LIVE",
                     sig.summary(), sig.parse_confidence, sig.limit_price)
            # Always mirror to paper account regardless of dry_run
            if bridge:
                bridge.open_trade(sig)
            if not dry_run and rule_engine:
                await rule_engine.validate_new_trade(sig)
        else:
            log.debug("Low-confidence signal from %s: %.80s", author, text)
    return handle


def make_update_handler(exit_parser, db, rule_engine, dry_run: bool):
    async def handle(msg: dict):
        text = _extract_text(msg)
        if not text:
            return
        exit_sig = exit_parser.parse(text)
        if exit_sig is None:
            return
        log.info("UPDATE: %s", exit_sig)
        if not exit_sig.symbol:
            return
        with sqlite3.connect(db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            candidates = conn.execute(
                "SELECT * FROM discord_signals WHERE paper_status='open' AND symbol=? ORDER BY received_at DESC",
                (exit_sig.symbol,)
            ).fetchall()
        if not candidates:
            log.debug("No open trade for %s", exit_sig.symbol)
            return
        trade = dict(candidates[0])
        if dry_run:
            log.info("   [DRY] Would evaluate exit: symbol=%s pnl=%s%% exit_price=%s",
                     exit_sig.symbol, exit_sig.pnl_pct, exit_sig.exit_price)
            return
        if rule_engine:
            await rule_engine.evaluate(
                trade_row=trade, pnl_pct=exit_sig.pnl_pct,
                exit_price=exit_sig.exit_price, message_text=text,
            )
    return handle


def make_forum_handler(parser, db, exit_parser, rule_engine, dry_run: bool, bridge=None):
    async def handle_thread_create(thread: dict, http: aiohttp.ClientSession):
        thread_name = thread.get("name", "")
        thread_id   = thread.get("id", "")
        if not thread_name:
            return
        log.info("FORUM THREAD: %s", thread_name)
        try:
            url = f"{GATEWAY_API}/channels/{thread_id}/messages?limit=1"
            async with http.get(url) as resp:
                if resp.status != 200:
                    return
                msgs = await resp.json()
                if not msgs:
                    return
                first_msg = msgs[0]
        except Exception as e:
            log.warning("Could not fetch thread first message: %s", e)
            return
        text      = _extract_text(first_msg)
        full_text = f"{thread_name}\n{text}".strip()
        message_id = str(first_msg.get("id", ""))
        author     = first_msg.get("author", {}).get("username", "unknown")
        sig = parser.parse(text=full_text, message_id=message_id,
                           channel_id=thread_id, author=author)
        if sig is None:
            log.debug("Forum thread not parsed: %s", thread_name[:60])
            return
        saved = db.save(sig)
        if not saved:
            return
        if sig.is_tradeable:
            log.info("FORUM ENTRY [%s] %s  confidence=%s  entry=$%s",
                     "DRY" if dry_run else "LIVE",
                     sig.summary(), sig.parse_confidence, sig.limit_price)
            # Always mirror to paper account regardless of dry_run
            if bridge:
                bridge.open_trade(sig)
            if not dry_run and rule_engine:
                await rule_engine.validate_new_trade(sig)
        else:
            log.debug("Forum low-confidence: %.80s", full_text)

    async def handle_thread_message(msg: dict):
        text      = _extract_text(msg)
        thread_id = str(msg.get("channel_id", ""))
        if not text:
            return
        with sqlite3.connect(db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            trade_row = conn.execute(
                "SELECT * FROM discord_signals WHERE paper_status='open' AND channel_id=?",
                (thread_id,)
            ).fetchone()
        if not trade_row:
            exit_sig = exit_parser.parse(text)
            if exit_sig and exit_sig.symbol:
                with sqlite3.connect(db.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    trade_row = conn.execute(
                        "SELECT * FROM discord_signals WHERE paper_status='open' AND symbol=? ORDER BY received_at DESC LIMIT 1",
                        (exit_sig.symbol,)
                    ).fetchone()
        if not trade_row:
            return
        exit_sig   = exit_parser.parse(text)
        pnl_pct    = exit_sig.pnl_pct   if exit_sig else None
        exit_price = exit_sig.exit_price if exit_sig else None
        trade_dict = dict(trade_row)
        if dry_run:
            is_ravish = rule_engine and rule_engine.is_ravish_exit(text)
            if exit_sig or is_ravish:
                log.info("   [DRY FORUM] Would evaluate exit for trade #%d: %s",
                         trade_dict["id"], text[:80])
            return
        if rule_engine:
            await rule_engine.evaluate(
                trade_row=trade_dict, pnl_pct=pnl_pct,
                exit_price=exit_price, message_text=text,
            )

    return handle_thread_create, handle_thread_message


class UserGateway:
    def __init__(self, token, signal_channel_id, update_channel_id, forum_channel_id,
                 entry_handler, update_handler, forum_thread_create_handler,
                 forum_message_handler, backfill_limit=50):
        self.token                       = token
        self.signal_channel_id           = signal_channel_id
        self.update_channel_id           = update_channel_id
        self.forum_channel_id            = forum_channel_id
        self.entry_handler               = entry_handler
        self.update_handler              = update_handler
        self.forum_thread_create_handler = forum_thread_create_handler
        self.forum_message_handler       = forum_message_handler
        self.backfill_limit              = backfill_limit
        self._sequence                   = None
        self._heartbeat_interval         = 41.25
        self._http: Optional[aiohttp.ClientSession] = None
        self._active_thread_ids: set     = set()

    async def run(self):
        await self._backfill_channel(self.signal_channel_id, self.entry_handler, "signal")
        await self._backfill_channel(self.update_channel_id, self.update_handler, "update")
        await self._backfill_forum(self.forum_channel_id)
        while True:
            try:
                await self._connect()
            except Exception as e:
                log.warning("Gateway disconnected: %s — reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def _backfill_channel(self, channel_id, handler, label):
        log.info("Backfilling %s channel %s (last %d)...", label, channel_id, self.backfill_limit)
        url = f"{GATEWAY_API}/channels/{channel_id}/messages?limit={min(self.backfill_limit, 100)}"
        async with self._http.get(url) as resp:
            if resp.status == 200:
                msgs = await resp.json()
            elif resp.status == 403:
                log.warning("Cannot read %s channel — check token access", label)
                return
            else:
                log.warning("Backfill %s failed: HTTP %s", label, resp.status)
                return
        for msg in reversed(msgs):
            await handler(msg)
        log.info("Backfill %s complete: %d messages", label, len(msgs))

    async def _backfill_forum(self, forum_channel_id):
        log.info("Backfilling income-trades forum %s...", forum_channel_id)
        url = f"{GATEWAY_API}/channels/{forum_channel_id}/threads/search?limit=20&sort_by=last_message_time"
        async with self._http.get(url) as resp:
            if resp.status != 200:
                log.warning("Forum backfill failed: HTTP %s", resp.status)
                return
            data = await resp.json()
        threads = data.get("threads", [])
        log.info("Forum: %d active threads found", len(threads))
        for thread in threads:
            thread_id = thread["id"]
            self._active_thread_ids.add(int(thread_id))
            await self.forum_thread_create_handler(thread, self._http)
            msgs_url = f"{GATEWAY_API}/channels/{thread_id}/messages?limit=10"
            async with self._http.get(msgs_url) as mr:
                if mr.status != 200:
                    continue
                msgs = await mr.json()
            for msg in reversed(msgs[1:]):
                await self.forum_message_handler(msg)
        log.info("Forum backfill complete")

    async def _connect(self):
        async with self._http.ws_connect(GATEWAY_URL) as ws:
            log.info("Connected to Discord Gateway")
            heartbeat_task = None
            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(raw.data)
                    op   = data.get("op")
                    seq  = data.get("s")
                    if seq:
                        self._sequence = seq
                    if op == 10:
                        self._heartbeat_interval = data["d"]["heartbeat_interval"] / 1000
                        if heartbeat_task:
                            heartbeat_task.cancel()
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                        await self._identify(ws)
                    elif op == OP_DISPATCH:
                        event = data.get("t")
                        d     = data.get("d", {})
                        if event == "READY":
                            user = d["user"]
                            log.info(
                                "Logged in as %s — watching:\n"
                                "   signal=%s\n   update=%s\n   forum=%s",
                                user["username"],
                                self.signal_channel_id,
                                self.update_channel_id,
                                self.forum_channel_id,
                            )
                        elif event == "MESSAGE_CREATE":
                            ch = int(d.get("channel_id", 0))
                            if ch == self.signal_channel_id:
                                await self.entry_handler(d)
                            elif ch == self.update_channel_id:
                                await self.update_handler(d)
                            elif ch in self._active_thread_ids:
                                await self.forum_message_handler(d)
                        elif event == "THREAD_CREATE":
                            parent_id = int(d.get("parent_id", 0))
                            if parent_id == self.forum_channel_id:
                                thread_id = int(d["id"])
                                self._active_thread_ids.add(thread_id)
                                await self.forum_thread_create_handler(d, self._http)
                        elif event == "THREAD_LIST_SYNC":
                            for thread in d.get("threads", []):
                                parent_id = int(thread.get("parent_id", 0))
                                if parent_id == self.forum_channel_id:
                                    self._active_thread_ids.add(int(thread["id"]))
                            log.info("Forum thread sync: tracking %d threads",
                                     len(self._active_thread_ids))
                    elif op == OP_HEARTBEAT_ACK:
                        pass
                    elif op == OP_HEARTBEAT:
                        await ws.send_str(json.dumps({"op": OP_HEARTBEAT, "d": self._sequence}))
                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            if heartbeat_task:
                heartbeat_task.cancel()

    async def _identify(self, ws):
        await ws.send_str(json.dumps({
            "op": OP_IDENTIFY,
            "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": {"os": "Mac OS X", "browser": "Chrome", "device": ""},
                "presence": {"status": "invisible", "afk": False},
                "compress": False,
            },
        }))

    async def _heartbeat_loop(self, ws):
        await asyncio.sleep(self._heartbeat_interval * 0.5)
        while True:
            await ws.send_str(json.dumps({"op": OP_HEARTBEAT, "d": self._sequence}))
            await asyncio.sleep(self._heartbeat_interval)


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token             = os.getenv("DISCORD_USER_TOKEN")
    signal_channel_id = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID",  "1418852039382925353"))
    update_channel_id = int(os.getenv("DISCORD_UPDATE_CHANNEL_ID",  "1466838176202231829"))
    forum_channel_id  = int(os.getenv("DISCORD_FORUM_CHANNEL_ID",   "1427087208657059952"))
    owner_id          = os.getenv("DISCORD_OWNER_ID", "1424445514212049118")
    db_path           = os.getenv("DB_PATH", "db/trading.db")
    dry_run           = os.getenv("DRY_RUN", "true").lower() != "false"
    backfill          = int(os.getenv("BACKFILL_LIMIT", "50"))
    rules_path        = os.getenv("EXIT_RULES_PATH", "discord_alerts/exit_rules.yaml")

    if not token:
        raise RuntimeError("DISCORD_USER_TOKEN not set in .env")

    from discord_alerts.discord_signal_reader  import AlertParser, SignalDB
    from discord_alerts.discord_exit_parser    import ExitParser
    from discord_alerts.discord_exit_rules     import ExitRuleEngine
    from discord_alerts.discord_notifier       import DiscordNotifier
    from discord_alerts.discord_paper_bridge   import DiscordPaperBridge

    parser      = AlertParser()
    db          = SignalDB(db_path=db_path)
    exit_parser = ExitParser()
    notifier    = DiscordNotifier(token=token, user_id=owner_id)
    bridge      = DiscordPaperBridge()
    rule_engine = ExitRuleEngine(
        rules_path = rules_path,
        db_path    = db_path,
        notifier   = notifier if not dry_run else None,
    )

    log.info("Paper bridge: account=%s  cash=$%.2f",
             bridge._account.account_id,
             bridge._account.get_state().cash)

    entry_handler  = make_entry_handler(parser, db, rule_engine, dry_run, bridge=bridge)
    update_handler = make_update_handler(exit_parser, db, rule_engine, dry_run)
    forum_thread_create, forum_message = make_forum_handler(
        parser, db, exit_parser, rule_engine, dry_run, bridge=bridge
    )

    log.info(
        "Starting OptionsKit listener  dry_run=%s\n"
        "  Signal channel  : %s\n"
        "  Update channel  : %s\n"
        "  Income-trades   : %s\n"
        "  DB              : %s\n"
        "  Exit rules      : %s\n"
        "  DM alerts to    : %s",
        dry_run, signal_channel_id, update_channel_id,
        forum_channel_id, db_path, rules_path, owner_id,
    )

    async def _run():
        async with aiohttp.ClientSession(
            headers={"Authorization": token, "User-Agent": "Mozilla/5.0"}
        ) as session:
            notifier.set_session(session)
            gateway = UserGateway(
                token                       = token,
                signal_channel_id           = signal_channel_id,
                update_channel_id           = update_channel_id,
                forum_channel_id            = forum_channel_id,
                entry_handler               = entry_handler,
                update_handler              = update_handler,
                forum_thread_create_handler = forum_thread_create,
                forum_message_handler       = forum_message,
                backfill_limit              = backfill,
            )
            gateway._http = session
            await gateway.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
