"""
broker_client/risk/stop_distance_sizer.py

Stop-distance based position sizing.

Formula:
    dollar_risk   = portfolio_value × risk_pct_per_trade   (default 0.5%)
    stop_distance = entry_price × stop_pct                 (default 8%)
    shares        = dollar_risk / (entry_price × stop_pct)
    position_$    = shares × entry_price

For options:
    dollar_risk   = portfolio_value × risk_pct_per_trade
    contracts     = dollar_risk / (premium × 100)
    (stop_pct on options = 50% of premium — options can go to 0)

Usage:
    from broker_client.risk.stop_distance_sizer import StopDistanceSizer

    sizer = StopDistanceSizer(portfolio_value=100_000)

    # Equity
    result = sizer.size_equity("AAPL", entry=180.0, stop=165.6)
    print(result.shares, result.position_dollars, result.risk_dollars)

    # Options
    result = sizer.size_option("AAPL", premium=3.50)
    print(result.contracts, result.position_dollars)
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── defaults from .env ────────────────────────────────────────────────────────
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default

RISK_PCT_PER_TRADE  = _env_float("RISK_PCT_PER_TRADE",  0.005)   # 0.5%
MAX_RISK_PCT        = _env_float("MAX_RISK_PCT",         0.01)    # 1% hard cap
MAX_POSITION_PCT    = _env_float("MAX_POSITION_PCT",     0.05)    # 5% of portfolio
DEFAULT_STOP_PCT    = _env_float("DEFAULT_STOP_PCT",     0.08)    # 8% below entry
OPTIONS_STOP_PCT    = _env_float("OPTIONS_STOP_PCT",     0.50)    # 50% of premium


# ── result dataclass ─────────────────────────────────────────────────────────
@dataclass
class SizeResult:
    symbol: str
    instrument_type: str          # "equity" | "option"
    entry_price: float
    stop_price: float
    stop_pct: float               # distance as fraction
    risk_dollars: float
    position_dollars: float
    shares: Optional[int] = None       # equity only
    contracts: Optional[int] = None    # options only
    capped_by: Optional[str] = None    # "max_position" | "max_risk" | None
    warning: Optional[str] = None

    @property
    def r_multiple(self) -> float:
        """How many $ risked per $ of position (inverse of R)."""
        if self.position_dollars == 0:
            return 0.0
        return self.risk_dollars / self.position_dollars


@dataclass
class SizerConfig:
    portfolio_value: float
    risk_pct: float = RISK_PCT_PER_TRADE
    max_risk_pct: float = MAX_RISK_PCT
    max_position_pct: float = MAX_POSITION_PCT
    default_stop_pct: float = DEFAULT_STOP_PCT
    options_stop_pct: float = OPTIONS_STOP_PCT


# ── main class ────────────────────────────────────────────────────────────────
class StopDistanceSizer:
    """
    Position sizing based on stop distance.

    Always stays within:
      - risk_pct_per_trade (default 0.5%)
      - max_risk_pct (hard cap 1%)
      - max_position_pct (5% of portfolio)
    """

    def __init__(
        self,
        portfolio_value: float,
        risk_pct: float = RISK_PCT_PER_TRADE,
        max_risk_pct: float = MAX_RISK_PCT,
        max_position_pct: float = MAX_POSITION_PCT,
        default_stop_pct: float = DEFAULT_STOP_PCT,
        options_stop_pct: float = OPTIONS_STOP_PCT,
    ):
        self.cfg = SizerConfig(
            portfolio_value=portfolio_value,
            risk_pct=risk_pct,
            max_risk_pct=max_risk_pct,
            max_position_pct=max_position_pct,
            default_stop_pct=default_stop_pct,
            options_stop_pct=options_stop_pct,
        )

    # ── public ─────────────────────────────────────────────────────────────
    def size_equity(
        self,
        symbol: str,
        entry: float,
        stop: Optional[float] = None,
        stop_pct: Optional[float] = None,
    ) -> SizeResult:
        """
        Size an equity position.

        Provide either:
          - stop (absolute price): stop=165.60
          - stop_pct (fraction below entry): stop_pct=0.08
        Default: stop_pct = DEFAULT_STOP_PCT
        """
        if entry <= 0:
            raise ValueError(f"Entry price must be positive, got {entry}")

        if stop is not None and stop > 0:
            _stop_pct = (entry - stop) / entry
            _stop = stop
        elif stop_pct is not None and stop_pct > 0:
            _stop_pct = stop_pct
            _stop = entry * (1 - stop_pct)
        else:
            _stop_pct = self.cfg.default_stop_pct
            _stop = entry * (1 - _stop_pct)

        if _stop_pct <= 0:
            raise ValueError(f"Stop must be below entry; got stop_pct={_stop_pct:.3f}")

        # Dollar risk budget
        target_risk = self.cfg.portfolio_value * self.cfg.risk_pct
        max_risk    = self.cfg.portfolio_value * self.cfg.max_risk_pct
        dollar_risk = min(target_risk, max_risk)

        # Shares from risk
        risk_per_share = entry * _stop_pct
        if risk_per_share <= 0:
            raise ValueError("risk_per_share must be positive")

        shares_float = dollar_risk / risk_per_share
        shares = max(1, int(shares_float))  # round down, minimum 1

        position = shares * entry
        max_pos   = self.cfg.portfolio_value * self.cfg.max_position_pct
        capped_by = None
        warning = None

        if position > max_pos:
            shares = max(1, int(max_pos / entry))
            position = shares * entry
            capped_by = "max_position"
            warning = (
                f"Position capped at {self.cfg.max_position_pct:.0%} of portfolio "
                f"(${max_pos:,.0f})"
            )

        actual_risk = shares * risk_per_share
        log.info(
            "Size %s: %d shares @ $%.2f  stop=$%.2f (%.1f%%)  "
            "position=$%.0f  risk=$%.0f (%.2f%%)",
            symbol, shares, entry, _stop, _stop_pct * 100,
            position, actual_risk, actual_risk / self.cfg.portfolio_value * 100,
        )

        return SizeResult(
            symbol=symbol,
            instrument_type="equity",
            entry_price=entry,
            stop_price=_stop,
            stop_pct=_stop_pct,
            risk_dollars=actual_risk,
            position_dollars=position,
            shares=shares,
            capped_by=capped_by,
            warning=warning,
        )

    def size_option(
        self,
        symbol: str,
        premium: float,
        stop_pct: Optional[float] = None,
    ) -> SizeResult:
        """
        Size an options position (long options only).

        stop_pct defaults to OPTIONS_STOP_PCT (50% of premium —
        i.e. exit when option loses half its value).
        """
        if premium <= 0:
            raise ValueError(f"Premium must be positive, got {premium}")

        _stop_pct = stop_pct if stop_pct is not None else self.cfg.options_stop_pct
        stop_price = premium * (1 - _stop_pct)

        target_risk = self.cfg.portfolio_value * self.cfg.risk_pct
        max_risk    = self.cfg.portfolio_value * self.cfg.max_risk_pct
        dollar_risk = min(target_risk, max_risk)

        # 1 contract = 100 shares
        risk_per_contract = premium * _stop_pct * 100
        if risk_per_contract <= 0:
            raise ValueError("risk_per_contract must be positive")

        contracts_float = dollar_risk / risk_per_contract
        contracts = max(1, int(contracts_float))

        position = contracts * premium * 100
        max_pos  = self.cfg.portfolio_value * self.cfg.max_position_pct
        capped_by = None
        warning = None

        if position > max_pos:
            contracts = max(1, int(max_pos / (premium * 100)))
            position = contracts * premium * 100
            capped_by = "max_position"
            warning = (
                f"Contracts capped at max_position {self.cfg.max_position_pct:.0%}"
            )

        actual_risk = contracts * risk_per_contract
        log.info(
            "Size %s option: %d contracts @ $%.2f premium  "
            "position=$%.0f  risk=$%.0f (%.2f%%)",
            symbol, contracts, premium, position,
            actual_risk, actual_risk / self.cfg.portfolio_value * 100,
        )

        return SizeResult(
            symbol=symbol,
            instrument_type="option",
            entry_price=premium,
            stop_price=stop_price,
            stop_pct=_stop_pct,
            risk_dollars=actual_risk,
            position_dollars=position,
            contracts=contracts,
            capped_by=capped_by,
            warning=warning,
        )

    def adjust_for_regime(
        self, result: SizeResult, regime: str
    ) -> SizeResult:
        """
        Scale position size down in uncertain/bear regimes.
        Call after size_equity() or size_option().

        regime: "bull" | "neutral" | "bear"
        """
        scale = {"bull": 1.0, "neutral": 0.75, "bear": 0.5}.get(regime, 1.0)
        if scale == 1.0:
            return result

        log.info("Regime=%s → scaling position by %.0f%%", regime, scale * 100)
        if result.shares is not None:
            result.shares = max(1, int(result.shares * scale))
            result.position_dollars = result.shares * result.entry_price
            result.risk_dollars *= scale
        if result.contracts is not None:
            result.contracts = max(1, int(result.contracts * scale))
            result.position_dollars = result.contracts * result.entry_price * 100
            result.risk_dollars *= scale

        if result.warning:
            result.warning += f" | Scaled to {scale:.0%} for {regime} regime"
        else:
            result.warning = f"Scaled to {scale:.0%} for {regime} regime"
        return result


# ── helper: pull portfolio value from Schwab ─────────────────────────────────
def get_portfolio_value_from_schwab(db_path: str = "trading.db") -> float:
    """Read latest cash + positions value from DB as best estimate."""
    try:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT SUM(market_value)
                   FROM positions
                   WHERE status = 'open'"""
            ).fetchone()
            position_value = float(row[0] or 0) if row else 0.0
            row2 = conn.execute(
                """SELECT cash FROM account_snapshot
                   ORDER BY recorded_at DESC LIMIT 1"""
            ).fetchone()
            cash = float(row2[0] or 0) if row2 else 0.0
            return cash + position_value
    except Exception as e:
        log.warning("Could not read portfolio value from DB: %s", e)
        return float(os.getenv("PAPER_ACCOUNT_VALUE", 100_000))


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    portfolio = float(sys.argv[1]) if len(sys.argv) > 1 else 100_000.0
    sizer = StopDistanceSizer(portfolio_value=portfolio)

    print(f"\nPortfolio: ${portfolio:,.0f}")
    print(f"Risk per trade: {sizer.cfg.risk_pct:.1%}  max: {sizer.cfg.max_risk_pct:.1%}")
    print()

    examples = [
        ("AAPL",  180.0, 165.6, None, "equity"),
        ("SPY",   450.0, 414.0, None, "equity"),
        ("NVDA",   90.0, None,  0.10, "equity"),
        ("TSLA",    5.50, None, None, "option"),
        ("AMZN",   2.80, None, None, "option"),
    ]

    print(f"{'Symbol':<8} {'Type':<8} {'Entry':>8} {'Stop':>8} {'Stop%':>7} {'Shares/Ct':>10} {'Position':>10} {'Risk$':>8}")
    print("-" * 80)
    for sym, entry, stop, spct, itype in examples:
        if itype == "equity":
            r = sizer.size_equity(sym, entry, stop=stop, stop_pct=spct)
            ct = f"{r.shares} sh"
        else:
            r = sizer.size_option(sym, entry)
            ct = f"{r.contracts} ct"
        print(
            f"{sym:<8} {itype:<8} {entry:>8.2f} {r.stop_price:>8.2f} "
            f"{r.stop_pct:>6.1%} {ct:>10} {r.position_dollars:>10,.0f} {r.risk_dollars:>8,.0f}"
        )
        if r.warning:
            print(f"          ⚠️  {r.warning}")
