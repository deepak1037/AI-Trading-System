"""Alert engine — Twilio SMS, Slack webhook, SendGrid email (Day 5).

Deduplication: no repeat of same alert type within ALERT_DEDUP_MINUTES.
Severity routing per CLAUDE.md Section 13:
  - Critical → SMS (Twilio)
  - High     → Slack webhook
  - Hourly   → Email digest (SendGrid)
  - EOD      → Email (SendGrid)
  - RiskError → SMS (immediate, bypasses dedup)
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import UTC, datetime
from typing import ClassVar

from config.settings import settings
from core.exceptions import DataError, TradingSystemError
from core.logger import get_logger
from core.retry import circuit_breaker, retry

logger = get_logger(__name__)

# Discord embed colors (decimal).
_DISCORD_RED = 16711680     # critical → #alerts
_DISCORD_YELLOW = 16776960  # high → #signals
_DISCORD_GREEN = 65280      # opportunity → #opportunities
_DISCORD_BLUE = 3447003     # daily briefing → #daily-briefing


def _dedup_key(channel: str, severity: str, message_hash: str) -> str:
    return f"{channel}:{severity}:{message_hash}"


class AlertEngine:
    """Routes alerts to the appropriate channel(s) based on severity.

    Deduplication is in-memory (reset on restart) + SQLite for persistence.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._dedup: dict[str, float] = {}  # key → last_sent_epoch

    def _is_duplicate(self, key: str) -> bool:
        last = self._dedup.get(key)
        if last is None:
            return False
        age_minutes = (time.time() - last) / 60.0
        return age_minutes < settings.ALERT_DEDUP_MINUTES

    def _record_sent(self, key: str) -> None:
        self._dedup[key] = time.time()

    def _log_to_db(
        self,
        channel: str,
        severity: str,
        message: str,
        signal_id: int | None = None,
    ) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """INSERT INTO alerts_log (signal_id, channel, severity, message, sent_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (signal_id, channel, severity, message, datetime.now(tz=UTC).isoformat()),
                )
        except Exception as exc:
            logger.warning("Failed to log alert to DB: %s", exc)

    # ── SMS via Twilio ───────────────────────────────────────────────────────

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
    )
    def _send_sms(self, message: str) -> bool:
        if not all([settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN,
                    settings.TWILIO_FROM_NUMBER, settings.TWILIO_TO_NUMBER]):
            # TODO: Configure TWILIO_* credentials in .env for SMS alerts
            logger.warning("Twilio not configured — SMS alert suppressed: %s", message[:80])
            return False
        try:
            from twilio.rest import Client

            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            client.messages.create(
                body=message[:1600],
                from_=settings.TWILIO_FROM_NUMBER,
                to=settings.TWILIO_TO_NUMBER,
            )
            logger.info("SMS sent: %s", message[:80])
            return True
        except ImportError:
            logger.warning("twilio package not installed — SMS suppressed")
            return False
        except Exception as exc:
            raise DataError(f"Twilio SMS failed: {exc}") from exc

    # ── Slack webhook ────────────────────────────────────────────────────────

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    def _send_slack(self, message: str, emoji: str = ":chart_with_upwards_trend:") -> bool:
        if not settings.SLACK_WEBHOOK_URL:
            # TODO: Configure SLACK_WEBHOOK_URL in .env for Slack alerts
            logger.warning("Slack webhook not configured — alert suppressed")
            return False
        try:
            import requests

            payload = {"text": f"{emoji} *Trading Alert*\n{message}"}
            resp = requests.post(settings.SLACK_WEBHOOK_URL, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Slack alert sent")
            return True
        except Exception as exc:
            raise DataError(f"Slack webhook failed: {exc}") from exc

    # ── Email via SendGrid ───────────────────────────────────────────────────

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    def _send_email(self, subject: str, body: str) -> bool:
        if not all([settings.SENDGRID_API_KEY, settings.SENDGRID_FROM_EMAIL,
                    settings.SENDGRID_TO_EMAIL]):
            # TODO: Configure SENDGRID_* credentials in .env for email alerts
            logger.warning("SendGrid not configured — email suppressed: %s", subject)
            return False
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            msg = Mail(
                from_email=settings.SENDGRID_FROM_EMAIL,
                to_emails=settings.SENDGRID_TO_EMAIL,
                subject=subject,
                html_content=f"<pre>{body}</pre>",
            )
            sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
            sg.send(msg)
            logger.info("Email sent: %s", subject)
            return True
        except ImportError:
            logger.warning("sendgrid package not installed — email suppressed")
            return False
        except Exception as exc:
            raise DataError(f"SendGrid email failed: {exc}") from exc

    # ── Discord webhooks ─────────────────────────────────────────────────────

    @staticmethod
    def _discord_message(
        title: str, body: str, color: int, fields: list | None = None
    ) -> dict:
        return {"title": title, "body": body, "color": color, "fields": fields or []}

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    def _send_discord(self, webhook_url: str, message: dict) -> bool:
        """Send rich embed message to Discord webhook."""
        if not settings.DISCORD_ENABLED:
            logger.debug("Discord disabled — suppressed: %s", message.get("title"))
            return False
        if not webhook_url:
            logger.debug("Discord webhook not configured — suppressed: %s", message.get("title"))
            return False
        try:
            import requests

            payload = {
                "embeds": [{
                    "title": message["title"],
                    "description": message["body"],
                    "color": message["color"],
                    # green=65280, yellow=16776960, red=16711680, blue=3447003
                    "fields": message.get("fields", []),
                    "footer": {"text": f"AI Trading System | {datetime.now().strftime('%H:%M ET')}"},
                }]
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            # Discord returns 204 No Content on success (200 with ?wait=true).
            ok = resp.status_code in (200, 204)
            if ok:
                logger.info("Discord message sent: %s", message["title"])
            else:
                logger.warning(
                    "Discord webhook returned %d: %s", resp.status_code, resp.text[:120]
                )
            return ok
        except Exception as exc:
            raise DataError(f"Discord webhook failed: {exc}") from exc

    def _discord_route(
        self,
        webhook_url: str,
        channel: str,
        severity: str,
        title: str,
        body: str,
        color: int,
        fields: list | None = None,
        signal_id: int | None = None,
        dedup: bool = True,
    ) -> bool:
        """Dedup + send a Discord embed to a channel, then log it."""
        if not settings.DISCORD_ENABLED:
            return False
        key = _dedup_key(
            channel, severity, hashlib.md5(f"{title}{body}".encode()).hexdigest()[:8]
        )
        if dedup and self._is_duplicate(key):
            logger.debug("Dedup: suppressing %s Discord", channel)
            return False
        sent = self._send_discord(webhook_url, self._discord_message(title, body, color, fields))
        if sent:
            self._record_sent(key)
            self._log_to_db(channel, severity, title, signal_id)
        return sent

    def send_discord_critical(
        self, title: str, body: str, fields: list | None = None,
        signal_id: int | None = None,
    ) -> bool:
        """CRITICAL → #alerts (red embed)."""
        return self._discord_route(
            settings.DISCORD_WEBHOOK_ALERTS, "discord_alerts", "critical",
            title, body, _DISCORD_RED, fields, signal_id,
        )

    def send_discord_signal(
        self, title: str, body: str, fields: list | None = None,
        signal_id: int | None = None,
    ) -> bool:
        """HIGH → #signals (yellow embed)."""
        return self._discord_route(
            settings.DISCORD_WEBHOOK_SIGNALS, "discord_signals", "high",
            title, body, _DISCORD_YELLOW, fields, signal_id,
        )

    def send_discord_opportunity(
        self, title: str, body: str, fields: list | None = None,
        signal_id: int | None = None,
    ) -> bool:
        """Opportunity → #opportunities (green embed)."""
        return self._discord_route(
            settings.DISCORD_WEBHOOK_OPPORTUNITIES, "discord_opportunities", "opportunity",
            title, body, _DISCORD_GREEN, fields, signal_id,
        )

    def send_discord_briefing(
        self, title: str, body: str, fields: list | None = None,
    ) -> bool:
        """Daily briefing → #daily-briefing (blue embed). No dedup — fires once."""
        return self._discord_route(
            settings.DISCORD_WEBHOOK_BRIEFING, "discord_briefing", "briefing",
            title, body, _DISCORD_BLUE, fields, dedup=False,
        )

    # ── Public API ───────────────────────────────────────────────────────────

    # Channel name → (webhook, severity tag) and color name → embed int.
    _CHANNEL_MAP: ClassVar[dict[str, str]] = {
        "alerts": "discord_alerts",
        "signals": "discord_signals",
        "opportunities": "discord_opportunities",
        "briefing": "discord_briefing",
    }
    _COLOR_MAP: ClassVar[dict[str, int]] = {
        "red": _DISCORD_RED,
        "yellow": _DISCORD_YELLOW,
        "green": _DISCORD_GREEN,
        "blue": _DISCORD_BLUE,
    }

    def send_alert(
        self,
        title: str,
        body: str,
        channel: str = "signals",
        color: str = "yellow",
        fields: list | None = None,
        signal_id: int | None = None,
        dedup: bool = True,
    ) -> bool:
        """Generic Discord alert — used by Phase 2 (DTE alerts, exit signals).

        ``channel`` ∈ {alerts, signals, opportunities, briefing}; ``color`` ∈
        {red, yellow, green, blue}. Every Phase 2 alert routes through here so
        Discord is never called directly (CLAUDE_PHASE2 note 9).
        """
        webhook = {
            "alerts": settings.DISCORD_WEBHOOK_ALERTS,
            "signals": settings.DISCORD_WEBHOOK_SIGNALS,
            "opportunities": settings.DISCORD_WEBHOOK_OPPORTUNITIES,
            "briefing": settings.DISCORD_WEBHOOK_BRIEFING,
        }.get(channel, settings.DISCORD_WEBHOOK_SIGNALS)
        channel_tag = self._CHANNEL_MAP.get(channel, "discord_signals")
        color_int = self._COLOR_MAP.get(color, _DISCORD_YELLOW)
        return self._discord_route(
            webhook, channel_tag, channel, title, body, color_int,
            fields=fields, signal_id=signal_id, dedup=dedup,
        )

    def send_critical(
        self,
        message: str,
        signal_id: int | None = None,
        bypass_dedup: bool = False,
    ) -> bool:
        """Send critical alert via SMS. Bypasses dedup for RiskError."""
        key = _dedup_key("sms", "critical", hashlib.md5(message.encode()).hexdigest()[:8])
        if not bypass_dedup and self._is_duplicate(key):
            logger.debug("Dedup: suppressing critical SMS")
            return False

        sent = self._send_sms(f"[CRITICAL] {message}")
        if sent:
            self._record_sent(key)
            self._log_to_db("sms", "critical", message, signal_id)
        # Fan out to Discord #alerts (already deduped above).
        if settings.DISCORD_ENABLED:
            self.send_discord_critical("🚨 Critical Alert", message, signal_id=signal_id)
        return sent

    def send_high(self, message: str, signal_id: int | None = None) -> bool:
        """Send high-severity alert via Slack."""
        key = _dedup_key("slack", "high", hashlib.md5(message.encode()).hexdigest()[:8])
        if self._is_duplicate(key):
            logger.debug("Dedup: suppressing high Slack alert")
            return False

        sent = self._send_slack(f"[HIGH] {message}", emoji=":warning:")
        if sent:
            self._record_sent(key)
            self._log_to_db("slack", "high", message, signal_id)
        # Fan out to Discord #signals.
        if settings.DISCORD_ENABLED:
            self.send_discord_signal("⚠️ Signal", message, signal_id=signal_id)
        return sent

    def send_digest(self, subject: str, body: str) -> bool:
        """Send hourly digest email via SendGrid."""
        key = _dedup_key("email", "digest", hashlib.md5(subject.encode()).hexdigest()[:8])
        if self._is_duplicate(key):
            logger.debug("Dedup: suppressing digest email")
            return False

        sent = self._send_email(subject, body)
        if sent:
            self._record_sent(key)
            self._log_to_db("email", "digest", f"{subject}: {body[:200]}")
        return sent

    def send_eod_report(self, body: str) -> bool:
        """Send EOD report via email (no dedup — fires once daily)."""
        subject = f"EOD Trading Report — {datetime.now(tz=UTC).strftime('%Y-%m-%d')}"
        sent = self._send_email(subject, body)
        if sent:
            self._log_to_db("email", "eod", body[:500])
        return sent

    def send_signal_alert(self, state: object, signal_id: int | None = None) -> bool:
        """Send a signal alert from a MarketState (Slack/SMS + Discord #signals).

        Routes by composite score: ≥CONFIDENCE_CRITICAL → critical, else high.
        """
        direction = getattr(state, "current_regime", getattr(state, "direction", "?"))
        composite = int(getattr(state, "composite_score", 0))
        confidence = int(getattr(state, "confidence", 0))
        msg = (
            f"Signal: direction={direction} | confidence={confidence} | "
            f"composite={composite}"
        )
        if composite >= settings.CONFIDENCE_CRITICAL:
            return self.send_critical(msg, signal_id=signal_id)
        return self.send_high(msg, signal_id=signal_id)

    # ── Phase 3: earnings + event-play opportunity alerts ─────────────────────

    def send_earnings_alert(self, opportunity: object) -> bool:
        """Fire an earnings opportunity to #opportunities (green).

        Duck-typed against ``EarningsOpportunity`` so alerts has no import
        dependency on the earnings package.
        """
        ticker = getattr(opportunity, "ticker", "?")
        strategy = getattr(opportunity, "strategy", "?")
        score = getattr(opportunity, "overall_score", 0)
        priority = getattr(opportunity, "priority", "?")
        iv = getattr(opportunity, "iv_analysis", None)
        trade = getattr(opportunity, "trade", None)
        llm = getattr(opportunity, "llm_assessment", None)

        lines = [
            f"**Earnings:** {getattr(opportunity, 'earnings_date', '?')} "
            f"{getattr(opportunity, 'earnings_time', '')}".strip(),
            f"**Strategy:** {strategy}",
        ]
        if iv is not None:
            lines += [
                f"**IV Rank:** {iv.iv_rank_current}/100",
                f"**Expected Move:** ±{iv.expected_move_current:.1f}%",
                f"**Avg IV Crush:** {iv.avg_iv_crush:.1f}%",
                f"**Breach Rate:** {iv.breach_rate:.0%}",
            ]
        lines += self._earnings_trade_lines(strategy, trade)
        if llm is not None:
            lines.append(
                f"\n**{getattr(llm, 'model', 'LLM')}:** {llm.recommendation} "
                f"({llm.confidence}% confidence)"
            )
            if getattr(llm, "reasoning", ""):
                lines.append(f"_{llm.reasoning}_")
        lines.append(f"\n**Score:** {score}/100 | **Priority:** {priority}")

        return self.send_alert(
            title=f"🎯 EARNINGS OPPORTUNITY: {ticker}",
            body="\n".join(lines),
            channel="opportunities",
            color="green",
        )

    @staticmethod
    def _earnings_trade_lines(strategy: str, trade: object) -> list[str]:
        if trade is None:
            return []
        out = ["\n**SUGGESTED TRADE:**"]
        if strategy == "IV_CRUSH":
            out += [
                f"  Sell {getattr(trade, 'put_strike', '?'):g}P @ "
                f"${getattr(trade, 'put_premium', 0):.2f}",
                f"  Margin: ${getattr(trade, 'margin_required', 0):,.0f}",
                f"  ROI: {getattr(trade, 'roi_margin', 0):.1%}",
                f"  Break-even: {getattr(trade, 'break_even_pct', 0):.1f}% drop needed",
            ]
        elif strategy == "IV_SPIKE":
            out += [
                f"  Buy {getattr(trade, 'instrument', 'straddle')}: "
                f"{getattr(trade, 'call_strike', '?'):g}C/"
                f"{getattr(trade, 'put_strike', '?'):g}P",
                f"  Cost: ${getattr(trade, 'total_premium_paid', 0):,.0f}",
                f"  ⚠️ EXIT {getattr(trade, 'exit_date', '?')} (1 day BEFORE earnings)",
            ]
        return out

    def send_bounce_alert(
        self,
        classification: object,
        bounce_score: object,
        trade_setup: object | None = None,
    ) -> bool:
        """Fire a bounce (event-play) opportunity to #opportunities (green)."""
        ticker = getattr(classification, "ticker", "?")
        body = [
            f"**Drop:** {getattr(classification, 'drop_pct', 0):.1f}% in "
            f"{settings.DROP_LOOKBACK_DAYS} days",
            f"**Cause:** {getattr(classification, 'classification', '?')} — "
            f"{getattr(classification, 'cause_summary', '')}",
            f"**Institutional:** {getattr(classification, 'institutional_action', 'neutral')}",
            f"**Bounce confidence:** {getattr(classification, 'bounce_confidence', 0)}%",
            f"**Overall score:** {getattr(bounce_score, 'overall_score', 0)}/100",
        ]
        if trade_setup is not None:
            body += [
                "\n**SUGGESTED TRADE (Bucket 3):**",
                f"  {getattr(trade_setup, 'instrument', '?')} — "
                f"target {getattr(trade_setup, 'profit_target_pct', 0):.0f}%",
                f"  Max capital: ${getattr(trade_setup, 'max_capital', 0):,.0f}",
                f"  Exit when: {getattr(trade_setup, 'thesis_complete_signal', '')}",
                f"\n⚡ Urgency: {getattr(trade_setup, 'entry_urgency', '?')}",
            ]
        return self.send_alert(
            title=f"📉 BOUNCE PLAY DETECTED: {ticker}",
            body="\n".join(body),
            channel="opportunities",
            color="green",
        )

    def handle_exception(self, exc: TradingSystemError) -> None:
        """Route exceptions to appropriate channel based on severity."""
        msg = f"{type(exc).__name__}: {exc}"
        if exc.is_critical:
            self.send_critical(msg, bypass_dedup=True)
        elif exc.severity == "HIGH":
            self.send_high(msg)
        else:
            logger.error("Alert (unrouted %s): %s", exc.severity, msg)

    def send_regime_alert(self, state: object) -> None:
        """Called by MarketStateTracker on regime transitions."""
        from signals.signal_schema import MarketState

        if not isinstance(state, MarketState):
            return
        msg = (
            f"Market regime change: {state.previous_regime} → {state.current_regime} "
            f"(composite={state.composite_score})"
        )
        if state.composite_score >= settings.CONFIDENCE_CRITICAL:
            self.send_critical(msg)
        elif state.composite_score >= settings.CONFIDENCE_HIGH:
            self.send_high(msg)
        else:
            logger.info("Regime transition (below alert threshold): %s", msg)


__all__ = ["AlertEngine"]
