"""BrokerFactory — returns the configured broker (Day 6)."""

from __future__ import annotations

from config.settings import settings
from core.exceptions import ConfigError
from core.logger import get_logger
from broker_core.base_broker import BaseBroker

logger = get_logger(__name__)


def get_broker() -> BaseBroker:
    """Return the broker configured by settings.BROKER."""
    broker_name = settings.BROKER.lower()
    logger.info("BrokerFactory: loading broker=%s", broker_name)

    if broker_name == "alpaca":
        from broker_core.alpaca_broker import AlpacaBroker
        return AlpacaBroker()
    elif broker_name == "schwab":
        from broker_core.schwab_broker import SchwabBroker
        return SchwabBroker()
    elif broker_name == "ibkr":
        # TODO: IBKRBroker not yet implemented (stub only)
        raise ConfigError("IBKR broker not yet implemented")
    else:
        raise ConfigError(f"Unknown broker: {broker_name!r}")


__all__ = ["get_broker"]
