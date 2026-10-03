"""
broker_client/risk/strategy_regime_filter.py

Per-strategy regime gate.  Before any signal is acted on, call:

    gate = StrategyRegimeFilter(db_path=..., vix=current_vix, spy_trend=...)
    if not gate.is_allowed("bucket3_leap"):
        log.info("Bucket3 blocked – bear regime")

Regime is derived from:
  1. VIX level   (fetch live or pass in)
  2. SPY 50/200 MA relationship stored in market_state_log
  3. Optional override in .env: REGIME_OVERRIDE=bull|bear|neutral

Each strategy bucket has a minimum allowed regime:
  bucket1_msp       → always (neutral + bull + bear)
  bucket1_wheel     → always
  bucket2_iv_crush  → always (VIX actually helps)
  bucket2_iv_spike  → neutral or bull only
  bucket3_leap      → bull only
  bucket3_event_bounce → neutral or bull only

Usage:
    from broker_client.risk.strategy_regime_filter import StrategyRegimeFilter
    gate = StrategyRegimeFilter(db_path="trading.db")
    allowed = gate.is_allowed("bucket3_leap")
"""

from __future__ import annotations

import os
import sqlite3
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


# ── regime enum ───────────────────────────────────────────────────────────────
class Regime(str, Enum):
    BULL    = "bull"
    NEUTRAL = "neutral"
    BEAR    = "bear"
    UNKNOWN = "unknown"


# ── per-strategy config ────────────────────────────────────────────────────────
# Tuple: minimum regimes where the strategy is ALLOWED (most restrictive first)
STRATEGY_REGIME_RULES: dict[str, list[Regime]] = {
    "bucket1_msp":          [Regime.BULL, Regime.NEUTRAL, Regime.BEAR],
    "bucket1_wheel":        [Regime.BULL, Regime.NEUTRAL, Regime.BEAR],
    "bucket2_iv_crush":     [Regime.BULL, Regime.NEUTRAL, Regime.BEAR],
    "bucket2_iv_spike":     [Regime.BULL, Regime.NEUTRAL],
    "bucket3_leap":         [Regime.BULL],
    "bucket3_event_bounce": [Regime.BULL, Regime.NEUTRAL],
}

# VIX thresholds
VIX_BULL_MAX    = 18.0   # below → bull signal from VIX
VIX_BEAR_MIN    = 30.0   # above → bear signal from VIX
VIX_FEAR_SPIKE  = 40.0   # above → block everything except msp+wheel


@dataclass
class RegimeDecision:
    regime: Regime
    vix: Optional[float]
    spy_above_200ma: Optional[bool]
    override: Optional[str]
    reason: str


class StrategyRegimeFilter:
    """
    Determines current market regime and gates strategies accordingly.

    Parameters
    ----------
    db_path : str
        Path to SQLite trading.db (reads market_state_log)
    vix : float, optional
        Current VIX — if None, reads from DB or defaults to neutral
    spy_above_200ma : bool, optional
        If None, reads from market_state_log table
    """

    def __init__(
        self,
        db_path: str = "trading.db",
        vix: Optional[float] = None,
        spy_above_200ma: Optional[bool] = None,
    ):
        self.db_path = db_path
        self._vix = vix
        self._spy_above_200ma = spy_above_200ma
        self._decision: Optional[RegimeDecision] = None  # cached

    # ── public api ─────────────────────────────────────────────────────────
    @property
    def regime(self) -> Regime:
        return self._get_decision().regime

    def is_allowed(self, strategy: str) -> bool:
        """Return True if the strategy is permitted under current regime."""
        d = self._get_decision()
        allowed_regimes = STRATEGY_REGIME_RULES.get(strategy, [Regime.BULL, Regime.NEUTRAL, Regime.BEAR])

        # Hard block: VIX fear spike blocks everything except msp/wheel
        if d.vix and d.vix >= VIX_FEAR_SPIKE:
            if strategy not in ("bucket1_msp", "bucket1_wheel"):
                log.warning(
                    "VIX=%.1f FEAR SPIKE — blocking %s", d.vix, strategy
                )
                return False

        allowed = d.regime in allowed_regimes
        if not allowed:
            log.info(
                "Strategy %s blocked: regime=%s, requires one of %s",
                strategy,
                d.regime.value,
                [r.value for r in allowed_regimes],
            )
        return allowed

    def gate_signals(self, signals: list[dict]) -> list[dict]:
        """
        Filter a list of signal dicts.  Each signal must have key 'strategy'.
        Returns only signals whose strategy is allowed.
        """
        return [s for s in signals if self.is_allowed(s.get("strategy", ""))]

    def explain(self) -> str:
        d = self._get_decision()
        lines = [
            f"Regime: {d.regime.value.upper()}",
            f"VIX: {d.vix:.1f}" if d.vix else "VIX: unknown",
            f"SPY > 200MA: {d.spy_above_200ma}",
            f"Reason: {d.reason}",
        ]
        if d.override:
            lines.append(f"⚠️  OVERRIDE active: {d.override}")
        lines.append("")
        lines.append("Strategy gates:")
        for strat, allowed_regimes in STRATEGY_REGIME_RULES.items():
            status = "✅" if self.is_allowed(strat) else "🔴"
            lines.append(f"  {status} {strat:<30} needs {[r.value for r in allowed_regimes]}")
        return "\n".join(lines)

    # ── internals ──────────────────────────────────────────────────────────
    def _get_decision(self) -> RegimeDecision:
        if self._decision:
            return self._decision
        self._decision = self._compute_regime()
        return self._decision

    def _compute_regime(self) -> RegimeDecision:
        # 1. Check .env override
        override = os.getenv("REGIME_OVERRIDE", "").strip().lower()
        if override in ("bull", "bear", "neutral"):
            return RegimeDecision(
                regime=Regime(override),
                vix=self._vix,
                spy_above_200ma=self._spy_above_200ma,
                override=override,
                reason=f"Manual .env REGIME_OVERRIDE={override}",
            )

        vix = self._resolve_vix()
        spy_above = self._resolve_spy_trend()

        # 2. Classify
        regime, reason = self._classify(vix, spy_above)

        return RegimeDecision(
            regime=regime,
            vix=vix,
            spy_above_200ma=spy_above,
            override=None,
            reason=reason,
        )

    def _classify(
        self,
        vix: Optional[float],
        spy_above_200ma: Optional[bool],
    ) -> tuple[Regime, str]:
        """Combine VIX + SPY trend into a regime label."""

        # No data → neutral (safe default)
        if vix is None and spy_above_200ma is None:
            return Regime.NEUTRAL, "No market data — defaulting to neutral"

        votes_bull = 0
        votes_bear = 0
        reasons = []

        if vix is not None:
            if vix < VIX_BULL_MAX:
                votes_bull += 1
                reasons.append(f"VIX={vix:.1f}<{VIX_BULL_MAX} (low fear)")
            elif vix >= VIX_BEAR_MIN:
                votes_bear += 2  # weight VIX more on fear spikes
                reasons.append(f"VIX={vix:.1f}>={VIX_BEAR_MIN} (high fear)")
            else:
                reasons.append(f"VIX={vix:.1f} neutral zone")

        if spy_above_200ma is True:
            votes_bull += 1
            reasons.append("SPY above 200MA (uptrend)")
        elif spy_above_200ma is False:
            votes_bear += 1
            reasons.append("SPY below 200MA (downtrend)")

        if votes_bull > votes_bear:
            return Regime.BULL, "; ".join(reasons)
        elif votes_bear > votes_bull:
            return Regime.BEAR, "; ".join(reasons)
        else:
            return Regime.NEUTRAL, "; ".join(reasons)

    def _resolve_vix(self) -> Optional[float]:
        if self._vix is not None:
            return self._vix
        # Try DB
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """SELECT vix FROM market_state_log
                       ORDER BY recorded_at DESC LIMIT 1"""
                ).fetchone()
                if row:
                    return float(row[0])
        except Exception as e:
            log.debug("VIX from DB failed: %s", e)

        # Try yfinance live
        try:
            import yfinance as yf
            vix_ticker = yf.Ticker("^VIX")
            hist = vix_ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            log.debug("VIX from yfinance failed: %s", e)

        return None

    def _resolve_spy_trend(self) -> Optional[bool]:
        if self._spy_above_200ma is not None:
            return self._spy_above_200ma
        # Try DB
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """SELECT spy_above_200ma FROM market_state_log
                       ORDER BY recorded_at DESC LIMIT 1"""
                ).fetchone()
                if row and row[0] is not None:
                    return bool(row[0])
        except Exception as e:
            log.debug("SPY trend from DB failed: %s", e)

        # Compute live
        try:
            import yfinance as yf
            import pandas as pd
            spy = yf.Ticker("SPY")
            hist = spy.history(period="1y")
            if len(hist) >= 200:
                ma200 = hist["Close"].rolling(200).mean().iloc[-1]
                current = hist["Close"].iloc[-1]
                return bool(current > ma200)
        except Exception as e:
            log.debug("SPY 200MA from yfinance failed: %s", e)

        return None


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else "trading.db"
    gate = StrategyRegimeFilter(db_path=db)
    print(gate.explain())
