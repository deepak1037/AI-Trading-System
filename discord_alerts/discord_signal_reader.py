
from __future__ import annotations
import asyncio, os, re, sqlite3, logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional
import discord

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

CREATE_DISCORD_SIGNALS = """
CREATE TABLE IF NOT EXISTS discord_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    UNIQUE,
    channel_id      TEXT,
    author          TEXT,
    raw_text        TEXT,
    received_at     TEXT,
    action          TEXT,
    symbol          TEXT,
    strategy_type   TEXT,
    strike          REAL,
    expiry_short    TEXT,
    expiry_long     TEXT,
    option_type     TEXT,
    limit_price     REAL,
    contracts       INTEGER DEFAULT 1,
    tos_code        TEXT,
    paper_status    TEXT    DEFAULT 'open',
    paper_entry     REAL,
    paper_exit      REAL,
    paper_pnl_pct   REAL,
    closed_at       TEXT,
    skip_reason     TEXT,
    parse_confidence TEXT
);
"""

@dataclass
class ParsedSignal:
    message_id: str
    channel_id: str
    author: str
    raw_text: str
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    action: Optional[str] = None
    symbol: Optional[str] = None
    strategy_type: str = "unknown"
    strike: Optional[float] = None
    expiry_short: Optional[str] = None
    expiry_long: Optional[str] = None
    option_type: Optional[str] = None
    limit_price: Optional[float] = None
    contracts: int = 1
    tos_code: Optional[str] = None
    paper_status: str = "open"
    paper_entry: Optional[float] = None
    paper_exit: Optional[float] = None
    paper_pnl_pct: Optional[float] = None
    closed_at: Optional[str] = None
    skip_reason: Optional[str] = None
    parse_confidence: str = "low"

    @property
    def is_tradeable(self):
        return self.symbol is not None and self.limit_price is not None and self.action is not None and self.parse_confidence in ("high","medium")

    def summary(self):
        parts = [self.action or "?", self.symbol or "?"]
        if self.strike: parts.append(str(self.strike))
        if self.option_type: parts.append(self.option_type)
        if self.expiry_short: parts.append(self.expiry_short)
        if self.limit_price: parts.append(f"@ {self.limit_price}")
        return " ".join(parts)

class AlertParser:
    MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06","JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

    def parse(self, text, message_id, channel_id, author):
        text = text.strip()
        if not self._has_trade_content(text):
            return None
        sig = ParsedSignal(message_id=message_id, channel_id=channel_id, author=author, raw_text=text)
        if self._parse_tos_code(text, sig):
            sig.parse_confidence = "high"
        elif self._parse_structured_bot(text, sig):
            sig.parse_confidence = "high"
        elif self._parse_simple_line(text, sig):
            sig.parse_confidence = "medium"
        else:
            sig.parse_confidence = "low"
            sig.skip_reason = "Could not parse trade details"
        return sig

    def _has_trade_content(self, text):
        keywords = ["STO","BTO","SELL","BUY","CALENDAR","DIAGONAL","SHORT PUT","SHORT CALL","LEAPS"]
        upper = text.upper()
        return any(k in upper for k in keywords)

    def _parse_tos_code(self, text, sig):
        m = re.search(r'(?:BUY|SELL)\s+[+-]\d+\s+(?:CALENDAR\s+|VERTICAL\s+|DIAGONAL\s+)?(\w+)\s+100.*?@([\d.]+)\s+LMT', text, re.IGNORECASE|re.DOTALL)
        if not m: return False
        sig.tos_code = m.group(0).strip()
        full = sig.tos_code.upper()
        sig.action = "BTO" if full.startswith("BUY") else "STO"
        sym_m = re.search(r'[+-]\d+\s+(?:CALENDAR\s+|VERTICAL\s+|DIAGONAL\s+)?(\w+)', full)
        if sym_m: sig.symbol = sym_m.group(1)
        sig.strategy_type = "calendar" if "CALENDAR" in full else "vertical" if "VERTICAL" in full else "diagonal" if "DIAGONAL" in full else "single"
        sig.option_type = "CALL" if "CALL" in full else "PUT" if "PUT" in full else None
        sig.limit_price = float(m.group(2))
        sig.paper_entry = sig.limit_price
        dates = re.findall(r'(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{2,4})', full)
        if len(dates) >= 2: sig.expiry_short = self._fmt(*dates[0]); sig.expiry_long = self._fmt(*dates[1])
        elif len(dates) == 1: sig.expiry_short = self._fmt(*dates[0])
        sm = re.search(r'([\d.]+)\s+(?:CALL|PUT)', full)
        if sm: sig.strike = float(sm.group(1))
        return True

    def _parse_structured_bot(self, text, sig):
        upper = text.upper()
        m = re.search(r'(BTO|STO|BUY|SELL)\s+([A-Z]+)\s+', upper)
        if not m: return False
        sig.action = "BTO" if m.group(1) in ("BTO","BUY") else "STO"
        sig.symbol = m.group(2)
        sig.strategy_type = "calendar" if "CALENDAR" in upper else "diagonal" if "DIAGONAL" in upper else "vertical" if "VERTICAL" in upper else "unknown"
        sig.option_type = "CALL" if "CALL" in upper else "PUT" if "PUT" in upper else None
        pm = re.search(r'limit\s+price\s*\n?\s*([\d.]+)', text, re.IGNORECASE)
        if pm: sig.limit_price = float(pm.group(1))
        else:
            pm2 = re.search(r'@\s*([\d.]+)\s*(?:debit|credit)', text, re.IGNORECASE)
            if pm2: sig.limit_price = float(pm2.group(1))
        if sig.limit_price: sig.paper_entry = sig.limit_price
        sm = re.search(r'(?:SELL|BUY)\s+[+-]\d+\s+([\d.]+)\s+(?:CALL|PUT)', upper)
        if sm: sig.strike = float(sm.group(1))
        exps = re.findall(r'\((\w+\s+\d+)\)', text)
        if len(exps) >= 2: sig.expiry_short = exps[0]; sig.expiry_long = exps[1]
        elif len(exps) == 1: sig.expiry_short = exps[0]
        return sig.symbol is not None and sig.limit_price is not None

    def _parse_simple_line(self, text, sig):
        m = re.search(r'\b(STO|BTO)\s+([A-Z]+)\s+([\d.]+)\s+(PUT|CALL|put|call)(?:\s+LEAPS)?\s*@\s*([\d.]+)', text, re.IGNORECASE)
        if m:
            sig.action = m.group(1).upper(); sig.symbol = m.group(2).upper()
            sig.strike = float(m.group(3)); sig.option_type = m.group(4).upper()
            sig.limit_price = float(m.group(5)); sig.paper_entry = sig.limit_price
            sig.strategy_type = "short_put_leaps" if "LEAPS" in text.upper() else "short_put" if sig.action=="STO" else "long_call"
            return True
        m2 = re.search(r'\b(STO|BTO)\s+([A-Z]+)\s+.*?@\s*([\d.]+)', text, re.IGNORECASE)
        if m2:
            sig.action = m2.group(1).upper(); sig.symbol = m2.group(2).upper()
            sig.limit_price = float(m2.group(3)); sig.paper_entry = sig.limit_price
            sig.strategy_type = "diagonal" if "DIAGONAL" in text.upper() else "calendar" if "CALENDAR" in text.upper() else "unknown"
            return True
        return False

    def _fmt(self, day, mon, year):
        m = self.MONTHS.get(mon.upper(),"00")
        yr = year if len(year)==4 else f"20{year}"
        return f"{yr}-{m}-{day.zfill(2)}"

class SignalDB:
    def __init__(self, db_path="trading.db"):
        self.db_path = db_path
        with sqlite3.connect(db_path) as conn:
            conn.execute(CREATE_DISCORD_SIGNALS)

    def save(self, sig):
        d = asdict(sig)
        cols = ", ".join(d.keys()); ph = ", ".join(["?"]*len(d))
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"INSERT INTO discord_signals ({cols}) VALUES ({ph})", list(d.values()))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_open_signals(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM discord_signals WHERE paper_status='open' ORDER BY received_at DESC").fetchall()
        return [dict(r) for r in rows]

    def close_signal(self, signal_id, exit_price, reason="manual"):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT paper_entry, action FROM discord_signals WHERE id=?", (signal_id,)).fetchone()
            if not row: return
            entry, action = row
            pnl_pct = ((entry - exit_price)/entry*100) if action=="STO" else ((exit_price-entry)/entry*100) if entry else None
            conn.execute("UPDATE discord_signals SET paper_status='closed',paper_exit=?,paper_pnl_pct=?,closed_at=?,skip_reason=? WHERE id=?",
                (exit_price, pnl_pct, datetime.now(timezone.utc).isoformat(), reason, signal_id))

class SignalBot(discord.Client):
    def __init__(self, channel_id, db, dry_run=True):
        intents = discord.Intents.default(); intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id = channel_id; self.db = db
        self.parser = AlertParser(); self.dry_run = dry_run; self._backfill_done = False

    async def on_ready(self):
        log.info("Bot connected as %s", self.user)
        if not self._backfill_done:
            await self._backfill_history(); self._backfill_done = True

    async def on_message(self, message):
        if message.channel.id != self.channel_id: return
        await self._process(message)

    async def _process(self, message):
        text = message.content or ""
        for embed in message.embeds:
            if embed.description: text += "\n" + embed.description
        if not text.strip(): return
        sig = self.parser.parse(text, str(message.id), str(message.channel.id), str(message.author))
        if sig is None: return
        if not self.db.save(sig): return
        if sig.is_tradeable:
            log.info("📥 [%s] %s  confidence=%s  entry=$%s", "DRY" if self.dry_run else "LIVE", sig.summary(), sig.parse_confidence, sig.limit_price)
        else:
            log.info("⚠️  Unparseable: %s... reason=%s", text[:60], sig.skip_reason)

    async def _backfill_history(self, limit=200):
        channel = self.get_channel(self.channel_id)
        if not channel: log.warning("Channel %s not found", self.channel_id); return
        log.info("Backfilling last %d messages...", limit)
        count = 0
        async for msg in channel.history(limit=limit):
            await self._process(msg); count += 1
        log.info("Backfill complete: %d messages", count)

def main():
    # Load .env automatically
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id = int(os.getenv("DISCORD_SIGNAL_CHANNEL_ID","1418852039382925353"))
    db_path = os.getenv("DB_PATH","trading.db")
    dry_run = os.getenv("DRY_RUN","true").lower() != "false"
    if not token: raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    log.info("Starting OptionsKit signal reader (dry_run=%s)", dry_run)
    db = SignalDB(db_path=db_path)
    bot = SignalBot(channel_id=channel_id, db=db, dry_run=dry_run)
    bot.run(token)

if __name__ == "__main__":
    main()
