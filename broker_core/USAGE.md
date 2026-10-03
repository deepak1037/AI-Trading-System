# broker_core — Brokerage Package Usage Guide

`broker_core` is a self-contained package. Any component in the system
(Discord listener, scanner, trading engine) can import it directly.

---

## Quick-start per component

### Discord listener
```python
from broker_core.account_registry import get_paper_account, get_broker_for
from config.settings import settings

# Paper account — isolated to discord signals, shared if name matches
paper = get_paper_account(settings.DISCORD_PAPER_ACCOUNT)  # "paper_discord"

# Real Schwab broker — uses DISCORD_SCHWAB_ACCOUNT or falls back to SCHWAB_ACCOUNT_NUMBER
broker = get_broker_for(settings.DISCORD_SCHWAB_ACCOUNT or None)

# Route an order
from broker_client.order_router import OrderRouter
router = OrderRouter(broker=broker, paper_account=paper)
result = router.execute(order)
```

### Multi-bagger scanner (paper-only — no live orders)
```python
from broker_core.account_registry import get_paper_account
from config.settings import settings

paper = get_paper_account(settings.SCANNER_PAPER_ACCOUNT)  # "paper_scanner"
# Scanner doesn't execute orders — it only reads account state for risk checks:
state = paper.get_state()
```

### Trading engine (the live watcher)
```python
from broker_core.account_registry import get_paper_account, get_broker_for
from config.settings import settings

paper  = get_paper_account(settings.TRADING_ENGINE_PAPER_ACCOUNT)  # "paper_main"
broker = get_broker_for(settings.TRADING_ENGINE_SCHWAB_ACCOUNT or None)

from broker_client.order_router import OrderRouter
router = OrderRouter(broker=broker, paper_account=paper)
```

---

## Shared accounts

Two components that share the **same** `.env` account name share state:

```
# .env
DISCORD_PAPER_ACCOUNT=paper_main
TRADING_ENGINE_PAPER_ACCOUNT=paper_main   ← same name → same JSON file
```

Both components see the same positions and cash. Use this when you want the
discord listener's paper trades to appear in the trading engine's portfolio.

Use **different** names (the default) to keep them isolated:

```
DISCORD_PAPER_ACCOUNT=paper_discord
TRADING_ENGINE_PAPER_ACCOUNT=paper_main
```

---

## Multiple real Schwab accounts

```python
# .env
SCHWAB_ACCOUNT_NUMBER=111111111           # default (trading engine)
TRADING_ENGINE_SCHWAB_ACCOUNT=111111111
DISCORD_SCHWAB_ACCOUNT=222222222          # separate account for discord alerts

# Code
from broker_core.account_registry import get_broker_for
broker_trading = get_broker_for("111111111")
broker_discord = get_broker_for("222222222")
```

Each `account_number` gets its own `SchwabBroker` instance with its own
cached account hash. The registry is a singleton — same account number
anywhere in the process → same object.

---

## Account names in .env

| Key | Default | Used by |
|-----|---------|---------|
| `TRADING_ENGINE_PAPER_ACCOUNT` | `paper_main` | Live market watcher + signal engine |
| `DISCORD_PAPER_ACCOUNT` | `paper_discord` | Discord signal listener |
| `SCANNER_PAPER_ACCOUNT` | `paper_scanner` | Multi-bagger scanner |
| `TRADING_ENGINE_SCHWAB_ACCOUNT` | *(blank → `SCHWAB_ACCOUNT_NUMBER`)* | Trading engine |
| `DISCORD_SCHWAB_ACCOUNT` | *(blank → `SCHWAB_ACCOUNT_NUMBER`)* | Discord listener |
| `SCHWAB_ACCOUNT_NUMBERS` | *(blank)* | Future multi-account enumeration |
