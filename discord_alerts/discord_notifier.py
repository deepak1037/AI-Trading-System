from __future__ import annotations
import logging
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)
DISCORD_API = "https://discord.com/api/v10"

class DiscordNotifier:
    def __init__(self, token: str, user_id: str):
        self.token   = token
        self.user_id = user_id
        self._dm_channel_id: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    def set_session(self, session):
        self._session = session

    async def _get_dm_channel(self):
        if self._dm_channel_id:
            return self._dm_channel_id
        try:
            async with self._session.post(
                f"{DISCORD_API}/users/@me/channels",
                json={"recipient_id": self.user_id}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._dm_channel_id = data["id"]
                    return self._dm_channel_id
        except Exception as e:
            log.warning("DM channel error: %s", e)
        return None

    async def send(self, message: str) -> bool:
        if not self._session:
            return False
        ch = await self._get_dm_channel()
        if not ch:
            return False
        try:
            async with self._session.post(
                f"{DISCORD_API}/channels/{ch}/messages",
                json={"content": message}
            ) as resp:
                ok = resp.status in (200, 201)
                if ok:
                    log.info("DM sent: %s", message[:80])
                return ok
        except Exception as e:
            log.warning("DM error: %s", e)
            return False

    async def alert_pt_hit(self, symbol, action, pnl_pct, target_pct, dte, trade_id):
        dte_str = f"  DTE={dte}" if dte is not None else ""
        await self.send(
            f"🎯 **PT Hit** — {action} {symbol}  `+{pnl_pct:.1f}%` (target {target_pct:.0f}%){dte_str}\n"
            f"Trade #{trade_id} — close manually if conditions are right"
        )

    async def alert_dte_stop(self, symbol, action, pnl_pct, dte, trade_id, auto_closed):
        status  = "**AUTO-CLOSING**" if auto_closed else "⚠️ DTE ALERT (manual close needed)"
        pnl_str = f"`{pnl_pct:+.1f}%`" if pnl_pct is not None else "P&L unknown"
        await self.send(f"⏰ {status} — {action} {symbol}  DTE={dte}  {pnl_str}\nTrade #{trade_id}")

    async def alert_ravish_exit(self, symbol, pnl_pct, trade_id, trigger):
        pnl_str = f"  `{pnl_pct:+.1f}%`" if pnl_pct is not None else ""
        await self.send(f"✅ **Auto-closed** — {symbol}{pnl_str}\nRavish: \"{trigger}\"\nTrade #{trade_id}")

    async def alert_no_rule(self, symbol, action, strategy_type, trade_id):
        await self.send(
            f"⚠️ **No exit rule** — {action} {symbol} `{strategy_type}`\n"
            f"Trade #{trade_id} open with no auto-exit defined.\n"
            f"Add rule to `discord_alerts/exit_rules.yaml` and restart."
        )

    async def alert_position_limit(self, symbol, open_count, max_count):
        await self.send(
            f"🚫 **Position limit reached** — cannot open {symbol}\n"
            f"{open_count}/{max_count} positions currently open."
        )
