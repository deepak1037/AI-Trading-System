"""broker_core — universal broker wrapper package.

Public surface
--------------
    from broker_core.base_broker import BaseBroker, Order, OptionsOrder, OrderResult
    from broker_core.factory import get_broker
    from broker_core.account_registry import get_paper_account, get_broker_for

Any component (discord listener, scanner, trading engine) should use
``get_paper_account(settings.<COMPONENT>_PAPER_ACCOUNT)`` to get their
per-component paper account, and ``get_broker_for(settings.<COMPONENT>_SCHWAB_ACCOUNT)``
for a real Schwab broker scoped to the correct account number.
"""
