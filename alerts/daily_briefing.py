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

from broker_client.analytics.performance_attribution import PerformanceAttribution
from broker_client.buckets.bucket_manager import BucketManager
from broker_client.buckets.exit_rules import ExitRulesEngine
from broker_client.risk.concentration_checker import ConcentrationChecker
from broker_client.risk.portfolio_heat_map import PortfolioHeatMap
from broker_client.risk.regime_advisor import RegimeAdvisor
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
        # Phase 4 additions — injected lazily so old callers keep working
        self._heat_map = PortfolioHeatMap(paper_account)
        self._concentration = ConcentrationChecker(paper_account)
        self._regime_advisor = RegimeAdvisor()
        self._attribution = PerformanceAttribution(paper_account=paper_account)

    # ── public API ───────────────────────────────────────────────────────────
    def generate_briefing(self) -> str:
        """Build the full briefing text. Never raises — sections degrade."""
        now = datetime.now(tz=UTC)
        sections = [
            "🌅 GOOD MORNING — AI Trading System Daily Brief",
            now.strftime("%A %B %d, %Y"),
            _BAR,
            self._macro_section(),
            self._risk_section(),
            self._positions_section(),
            self._opportunities_section(),
            self._health_section(),
            self._bucket_section(),
            self._performance_section(),
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

    def _risk_section(self) -> str:
        """Phase 4: portfolio heat map + concentration alerts."""
        try:
            positions = self._open_positions()
            report = self._heat_map.generate(positions)
            alerts = self._concentration.check(positions)
            regime_advice = self._regime_advisor.advise(self._market_state)

            cash_icon = "⚠️" if report.cash_buffer_warning else "✅"
            sector_icon = "⚠️" if report.sector_warning else "✅"

            alert_lines = ""
            if alerts:
                alert_lines = "\n" + "\n".join(
                    f"   {'🚨' if a.severity == 'CRITICAL' else '⚠️'} {a.message}"
                    for a in alerts[:5]
                )

            return (
                f"🛡️ PORTFOLIO RISK:\n"
                f"   Net delta: {report.net_delta:+.2f} ({report.delta_direction})\n"
                f"   Cash buffer: {report.cash_buffer_pct:.1f}% {cash_icon}\n"
                f"   Max sector: {report.max_sector_name} {report.max_sector_pct:.1f}% {sector_icon}\n"
                f"   Strategy mode: {regime_advice.bucket1_adjustment.upper()} (regime: {regime_advice.regime})"
                f"{alert_lines}"
            )
        except Exception as exc:
            logger.debug("DailyBriefing risk section failed: %s", exc)
            return "🛡️ PORTFOLIO RISK: unavailable"

    def _performance_section(self) -> str:
        """Phase 4: MTD performance attribution."""
        try:
            report = self._attribution.generate_report("MTD")
            if report.insufficient_data:
                return "📈 PERFORMANCE (MTD): insufficient data (< 5 closed trades)"
            b = report.bucket_attribution
            return (
                f"📈 PERFORMANCE (MTD):\n"
                f"   Total return: {report.total_return_pct:+.2f}%\n"
                f"   vs SPY: {report.vs_spy:+.2f}%\n"
                f"   Win rate: {report.win_rate:.0f}%\n"
                f"   Profit factor: {report.profit_factor:.2f}\n"
                f"\n"
                f"   Bucket 1: ${b[1].realized_pnl:+,.0f}\n"
                f"   Bucket 2: ${b[2].realized_pnl:+,.0f}\n"
                f"   Bucket 3: ${b[3].realized_pnl:+,.0f}"
            )
        except Exception as exc:
            logger.debug("DailyBriefing performance section failed: %s", exc)
            return "📈 PERFORMANCE (MTD): unavailable"

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
