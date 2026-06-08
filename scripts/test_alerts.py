"""Send a test embed to each configured Discord channel and confirm webhooks.

Usage:
    python scripts/test_alerts.py
    make test-alerts
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alerts.alert_engine import AlertEngine  # noqa: E402
from config.settings import settings  # noqa: E402


def main() -> int:
    if not settings.DISCORD_ENABLED:
        print("DISCORD_ENABLED is False — set it True in .env to send Discord alerts.")
        return 1

    eng = AlertEngine()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = [
        {"name": "Status", "value": "✅ webhook reachable", "inline": True},
        {"name": "Source", "value": "make test-alerts", "inline": True},
    ]
    # Unique body per run so the dedup window doesn't swallow repeat tests.
    note = f"Routing test sent at {stamp}."

    channels = [
        ("#alerts (critical · red)", settings.DISCORD_WEBHOOK_ALERTS,
         lambda: eng.send_discord_critical("🚨 Test Critical Alert", note, fields)),
        ("#signals (high · yellow)", settings.DISCORD_WEBHOOK_SIGNALS,
         lambda: eng.send_discord_signal("⚠️ Test Signal", note, fields)),
        ("#opportunities (green)", settings.DISCORD_WEBHOOK_OPPORTUNITIES,
         lambda: eng.send_discord_opportunity("🟢 Test Opportunity", note, fields)),
        ("#daily-briefing (blue)", settings.DISCORD_WEBHOOK_BRIEFING,
         lambda: eng.send_discord_briefing("📊 Test Daily Briefing", note, fields)),
    ]

    print("Sending test embeds to Discord channels...\n")
    any_configured = False
    any_failed = False
    for label, url, send in channels:
        if not url:
            print(f"  SKIP  {label}: webhook not set in .env")
            continue
        any_configured = True
        try:
            ok = send()
            print(f"  {'OK  ' if ok else 'FAIL'}  {label}: {'message sent' if ok else 'send failed'}")
            any_failed = any_failed or not ok
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {label}: {exc}")
            any_failed = True

    print()
    if not any_configured:
        print("No Discord webhooks configured. Add DISCORD_WEBHOOK_* to .env, then re-run.")
        return 1
    if any_failed:
        print("Some channels failed — check the webhook URLs in .env.")
        return 1
    print("All configured Discord channels reachable — check your Discord server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
