"""BrokerFactory — returns the configured broker (Day 6).

Usage
-----
    from broker_core.factory import get_broker

    broker = get_broker()                            # uses settings.BROKER + default account
    broker = get_broker(account_number="12345678")  # Schwab: scope to specific account

For multi-account / multi-component setups, prefer the registry:

    from broker_core.account_registry import get_broker_for
    broker = get_broker_for(account_number="12345678")

The registry returns the same object for repeated calls with the same account number.
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings
from core.exceptions import ConfigError
from core.logger import get_logger
from broker_core.base_broker import BaseBroker

logger = get_logger(__name__)


def get_broker(account_number: Optional[str] = None) -> BaseBroker:
    """Return the broker configured by settings.BROKER.

    Parameters
    ----------
    account_number:
        For Schwab: override the default SCHWAB_ACCOUNT_NUMBER from settings.
        Lets multiple components each hold a broker scoped to a different real
        account (e.g. ``"1234"`` for trading, ``"5678"`` for options).
        Ignored for Alpaca (which uses API key / paper flag from settings).
    """
    broker_name = settings.BROKER.lower()
    logger.info(
        "BrokerFactory: loading broker=%s account=%s",
        broker_name,
        (account_number or settings.SCHWAB_ACCOUNT_NUMBER or "default")[-4:] if broker_name == "schwab" else "n/a",
    )

    if broker_name == "alpaca":
        from broker_core.alpaca_broker import AlpacaBroker
        return AlpacaBroker()
    elif broker_name == "schwab":
        from broker_core.schwab_broker import SchwabBroker
        return SchwabBroker(account_number=account_number)
    elif broker_name == "ibkr":
        # TODO: IBKRBroker not yet implemented (stub only)
        raise ConfigError("IBKR broker not yet implemented")
    else:
        raise ConfigError(f"Unknown broker: {broker_name!r}")


__all__ = ["get_broker"]
