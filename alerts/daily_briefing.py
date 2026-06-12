"""Daily morning briefing (Phase 2 Step 9) — fires 8:05 AM ET on trading days.

Collects macro regime, open positions (with DTE/exit flags), opportunities,
portfolio health, and per-bucket performance into a single Discord embed sent
to the #daily-briefing channel. Every data source is injected and independently
guarded, so a single failing source degrades to a placeholder rather than
breaking the whole brief.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from broker_client.buckets.bucket_manager import BucketManager
from broker_client.buckets.exit_rules import ExitRulesEngine
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_BAR = "━" * 40


class DailyBriefing:
    """Generates and sends the morning trading brief."""

    def __init__(
        self,
        alert_engine: Any = None,
        bucket_manager: BucketManager | None = None,
        paper_account: Any = None,
        position_watcher: Any = None,
        market_state: Any = None,
        exit_rules: ExitRulesEngine | None = None,
        events_provider: Any = None,
    ) -> None:
        self._alerts = alert_engine
        self._buckets = bucket_manager or BucketManager()
        self._paper = paper_account
        self._watcher = position_watcher
        self._market_state = market_state
        self._exit_rules = exit_rules or ExitRulesEngine()
        self._events_provider = events_provider

    # ── public API ───────────────────────────────────────────────────────────
    def generate_briefing(self) -> str:
        """Build the full briefing text. Never raises — sections degrade."""
        now = datetime.now(tz=UTC)
        sections = [
            "🌅 GOOD MORNING — AI Trading System Daily Brief",
            now.strftime("%A %B %d, %Y"),
            _BAR,
            self._macro_section(),
            self._positions_section(),
            self._opportunities_section(),
            self._health_section(),
            self._bucket_section(),
            _BAR,
            f"AI Trading System | {now.strftime('%H:%M ET')}",
        ]
        return "\n\n".join(s for s in sections if s)

    def send_briefing(self) -> bool:
        """Generate + send to Discord #daily-briefing (blue embed)."""
        body = self.generate_briefing()
        if self._alerts is None:
            logger.info("DailyBriefing: no AlertEngine — brief generated but not sent")
            return False
        try:
            return bool(self._alerts.send_alert(
                title="🌅 Daily Briefing",
                body=body,
                channel="briefing",
                color="blue",
                dedup=False,
            ))
        except Exception as exc:
            logger.error("DailyBriefing: send failed: %s", exc)
            return False

    # ── sections (each guarded) ──────────────────────────────────────────────
    def _macro_section(self) -> str:
        regime, score = "unknown", 0
        try:
            if self._market_state is not None:
                regime = getattr(self._market_state, "current_regime", "unknown")
                score = int(getattr(self._market_state, "composite_score", 0))
        except Exception as exc:
            logger.debug("DailyBriefing macro section failed: %s", exc)
        events = self._todays_events()
        return (
            f"📊 MACRO REGIME: {str(regime).upper()}\n"
            f"   Signal score: {score}/100\n"
            f"   Today's events: {', '.join(events) or 'None scheduled'}"
        )

    def _positions_section(self) -> str:
        positions = self._open_positions()
        lines = [f"💼 OPEN POSITIONS ({len(positions)} total):"]
        dte_alerts = []
        for p in positions:
            dte = self._dte(p)
            if dte is not None and dte <= settings.DTE_EXIT_THRESHOLD:
                ticker = p.get("ticker", "?") if isinstance(p, dict) else getattr(p, "ticker", "?")
                dte_alerts.append(f"   ⏰ {ticker}: {dte} DTE — review for exit")
        if dte_alerts:
            lines.extend(dte_alerts)
        elif positions:
            lines.append("   No positions inside the 21-DTE window.")
        else:
            lines.append("   No open positions.")
        return "\n".join(lines)

    def _opportunities_section(self) -> str:
        opps = []
        try:
            if self._events_provider is not None and hasattr(self._events_provider, "opportunities"):
                opps = list(self._events_provider.opportunities() or [])
        except Exception as exc:
            logger.debug("DailyBriefing opportunities failed: %s", exc)
        body = "\n".join(f"   • {o}" for o in opps) or "   None flagged today."
        return f"🎯 TODAY'S OPPORTUNITIES:\n{body}"

    def _health_section(self) -> str:
        cash_buffer = settings.cash_buffer_pct
        equity = "n/a"
        try:
            if self._paper is not None:
                perf = self._paper.get_performance()
                equity = f"${perf.current_equity:,.0f} ({perf.total_return_pct:+.2f}%)"
        except Exception as exc:
            logger.debug("DailyBriefing health failed: %s", exc)
        return (
            "🛡️ PORTFOLIO HEALTH:\n"
            f"   Account equity: {equity}\n"
            f"   Target cash buffer: {cash_buffer}%"
        )

    def _bucket_section(self) -> str:
        try:
            summary = self._buckets.get_bucket_summary()
        except Exception as exc:
            logger.debug("DailyBriefing bucket section failed: %s", exc)
            return "📈 BUCKET PERFORMANCE (MTD): unavailable"
        names = {1: "MSP    ", 2: "Earnings", 3: "Events  "}
        lines = ["📈 BUCKET PERFORMANCE (MTD):"]
        for b in (1, 2, 3):
            pnl = summary.get(b)
            if pnl is None:
                continue
            lines.append(
                f"   Bucket {b} ({names[b]}): {pnl.mtd_return:+.2f}%  "
                f"${pnl.premium_collected:,.0f} premium  "
                f"({pnl.open_positions} open)"
            )
        return "\n".join(lines)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _open_positions(self) -> list:
        try:
            if self._watcher is not None:
                positions = self._watcher.positions
                if isinstance(positions, dict):
                    return list(positions.values())
                return list(positions)
        except Exception as exc:
            logger.debug("DailyBriefing positions fetch failed: %s", exc)
        return []

    @staticmethod
    def _dte(position: Any) -> int | None:
        get = position.get if isinstance(position, dict) else lambda k, d=None: getattr(position, k, d)
        dte = get("dte_remaining", None)
        if dte is not None:
            return int(dte)
        expiry = get("expiry", None)
        if expiry:
            from broker_client.position_watcher import _days_to_expiry

            return _days_to_expiry(expiry)
        return None

    def _todays_events(self) -> list[str]:
        try:
            if self._events_provider is not None and hasattr(self._events_provider, "todays_events"):
                return list(self._events_provider.todays_events() or [])
        except Exception as exc:
            logger.debug("DailyBriefing events failed: %s", exc)
        return []


__all__ = ["DailyBriefing"]
