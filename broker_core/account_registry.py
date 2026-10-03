"""broker_core/account_registry.py — Shared account registry (singleton).

Any component in the system can call:
    get_paper_account("discord_paper")   → PaperAccount (shared if name already created)
    get_broker_for("schwab-123abc")      → SchwabBroker scoped to that account hash

Two components using the same account name get the SAME object — their JSON file
is the same, so cash/positions are shared across the process (useful for the
scanner and discord listener reading the same portfolio state).

Usage
-----
    from broker_core.account_registry import get_paper_account, get_broker_for

    # In discord listener:
    account = get_paper_account(settings.DISCORD_PAPER_ACCOUNT)

    # In scanner / trading engine:
    account = get_paper_account(settings.SCANNER_PAPER_ACCOUNT)

    # For real-money Schwab (multi-account):
    broker = get_broker_for(account_number=settings.SCHWAB_ACCOUNT_NUMBER)
"""

from __future__ import annotations

import threading
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)

# ── Thread-safe singletons ────────────────────────────────────────────────────
_paper_lock = threading.Lock()
_paper_registry: dict[str, "PaperAccount"] = {}  # type: ignore[name-defined]

_broker_lock = threading.Lock()
_broker_registry: dict[str, "BaseBroker"] = {}  # type: ignore[name-defined]


def get_paper_account(account_id: str, accounts_dir: Optional[str] = None) -> "PaperAccount":
    """Return the PaperAccount for ``account_id``, creating it if needed.

    Thread-safe. Two calls with the same ``account_id`` return the same object.
    The JSON file on disk is the single source of truth — if another process (or
    a previous run) already created it, state is loaded from there.
    """
    from paper_trading.paper_account import PaperAccount  # local import avoids circulars

    with _paper_lock:
        if account_id not in _paper_registry:
            logger.info("AccountRegistry: creating PaperAccount[%s]", account_id)
            _paper_registry[account_id] = PaperAccount(account_id, accounts_dir=accounts_dir)
        return _paper_registry[account_id]


def get_broker_for(account_number: Optional[str] = None) -> "BaseBroker":
    """Return a broker instance scoped to ``account_number``.

    For Schwab, each distinct account number gets its own SchwabBroker that
    resolves the correct account hash at construction time.  If ``account_number``
    is None or empty, the default SCHWAB_ACCOUNT_NUMBER from settings is used.

    Thread-safe. Same ``account_number`` → same broker object.
    """
    from broker_core.factory import get_broker  # local import avoids circulars

    from config.settings import settings

    acct = account_number or settings.SCHWAB_ACCOUNT_NUMBER or "default"

    with _broker_lock:
        if acct not in _broker_registry:
            logger.info("AccountRegistry: creating broker for account=%s broker=%s", acct, settings.BROKER)
            broker = get_broker(account_number=acct)
            _broker_registry[acct] = broker
        return _broker_registry[acct]


def list_paper_accounts() -> list[str]:
    """Return names of all PaperAccount instances currently in the registry."""
    with _paper_lock:
        return list(_paper_registry.keys())


def clear_registry() -> None:
    """Wipe the in-memory registry (for testing only)."""
    with _paper_lock:
        _paper_registry.clear()
    with _broker_lock:
        _broker_registry.clear()


__all__ = ["get_paper_account", "get_broker_for", "list_paper_accounts", "clear_registry"]
