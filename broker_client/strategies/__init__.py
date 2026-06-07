"""Strategy registry (Day 8, CLAUDE.md Section 9)."""

from __future__ import annotations

from broker_client.strategies.base_strategy import BaseStrategy
from broker_client.strategies.cash_secured_put import CashSecuredPut
from broker_client.strategies.covered_call import CoveredCall
from broker_client.strategies.equity_long_short import EquityLongShort
from broker_client.strategies.iron_condor import IronCondor
from broker_client.strategies.momentum_breakout import MomentumBreakout
from broker_client.strategies.protective_put import ProtectivePut

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "equity_long_short": EquityLongShort,
    "momentum_breakout": MomentumBreakout,
    "covered_call": CoveredCall,
    "cash_secured_put": CashSecuredPut,
    "protective_put": ProtectivePut,
    "iron_condor": IronCondor,
}


def load_enabled_strategies(names: list[str]) -> list[BaseStrategy]:
    """Instantiate strategies from the registry by name list."""
    strategies = []
    for name in names:
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            from core.logger import get_logger
            get_logger(__name__).warning("Unknown strategy: %s — skipping", name)
            continue
        strategies.append(cls())
    return strategies


__all__ = ["STRATEGY_REGISTRY", "load_enabled_strategies", "BaseStrategy"]
