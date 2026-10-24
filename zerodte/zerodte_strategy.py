"""zerodte/zerodte_strategy.py

Pure decision logic for the Ravish 0DTE QQQ put-selling strategy — no I/O, so
it is fully unit-testable. The runner (zerodte_runner.py) supplies the chain,
prices, account and broker; this module only decides.

Strategy (mechanical, from Ravish's video):
  - Each trading day at 09:45 ET, SELL-TO-OPEN one QQQ 0DTE put at ~30 delta.
  - Bracket the position:
      take-profit  = buy-to-close at (100 − TP%) of the credit  (TP%=95 → 5%)
      stop-loss    = buy-to-close at (100 + SL%) of the credit  (SL%=200 → 3×)
  - If neither triggers by the force-close time (15:45 ET), close manually.
  - Always be flat before 16:00 ET (after-hours assignment risk).
  - Optionally skip big event days (CPI / FOMC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from broker_core.base_broker import OptionsChain, OptionsContract

_OPTIONS_MULTIPLIER = 100


@dataclass
class ZeroDTEPlan:
    """A fully-specified 0DTE short-put trade plan."""
    contract: OptionsContract
    credit: float          # premium received per share (mid)
    tp_price: float        # buy-to-close take-profit, per share
    sl_price: float        # buy-to-close stop-loss, per share
    contracts: int

    @property
    def credit_total(self) -> float:
        return self.credit * self.contracts * _OPTIONS_MULTIPLIER

    @property
    def max_planned_loss(self) -> float:
        """Risk defined by the stop, not the margin: (sl − credit) × 100 × qty."""
        return (self.sl_price - self.credit) * self.contracts * _OPTIONS_MULTIPLIER


def _mid(contract: OptionsContract) -> float:
    if contract.bid and contract.ask:
        return (contract.bid + contract.ask) / 2
    return contract.last or 0.0


def select_put(chain: OptionsChain, target_delta: float = 0.30) -> Optional[OptionsContract]:
    """Pick the put whose delta is closest to −target_delta (30-delta put).

    Puts have negative delta; the config carries a positive magnitude.
    """
    tgt = -abs(target_delta)
    puts = [p for p in chain.puts if p.delta is not None]
    if not puts:
        return None
    return min(puts, key=lambda p: abs((p.delta or 0) - tgt))


def build_plan(contract: OptionsContract, tp_pct: float, sl_pct: float,
               contracts: int = 1) -> Optional[ZeroDTEPlan]:
    """Build the bracket plan from the selected contract and the TP/SL percents."""
    credit = _mid(contract)
    if credit <= 0:
        return None
    tp_price = credit * (1 - tp_pct / 100.0)   # e.g. 95% TP → 5% of credit
    sl_price = credit * (1 + sl_pct / 100.0)   # e.g. 200% SL → 3× credit
    return ZeroDTEPlan(contract=contract, credit=round(credit, 4),
                       tp_price=round(max(tp_price, 0.0), 4),
                       sl_price=round(sl_price, 4), contracts=contracts)


def exit_reason(current_price: float, plan: ZeroDTEPlan) -> Optional[str]:
    """Given the option's current price, return why to close — or None to hold.

    The position is SHORT the put, so we BUY it back cheaper (profit) or more
    expensive (loss). A lower price is good.
    """
    if current_price <= plan.tp_price:
        return "take_profit"
    if current_price >= plan.sl_price:
        return "stop_loss"
    return None


def realized_pnl(plan: ZeroDTEPlan, exit_price: float) -> float:
    """$ P&L of the short put: (credit − buyback) × 100 × contracts."""
    return (plan.credit - exit_price) * plan.contracts * _OPTIONS_MULTIPLIER


def size_contracts(equity: float, capital_per_contract: float,
                   max_contracts: int) -> int:
    """How many contracts the account can carry (≥1 if any room)."""
    if capital_per_contract <= 0:
        return max_contracts
    n = int(equity // capital_per_contract)
    return max(0, min(n, max_contracts))


def is_event_day(today: date, event_dates: list[str]) -> bool:
    """True if today is a configured skip day (CPI / FOMC etc.), YYYY-MM-DD."""
    iso = today.isoformat()
    return any((d or "").strip()[:10] == iso for d in event_dates)


__all__ = [
    "ZeroDTEPlan", "select_put", "build_plan", "exit_reason",
    "realized_pnl", "size_contracts", "is_event_day",
]
