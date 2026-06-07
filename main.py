"""Entry point (CLAUDE.md Section 4): starts watcher + position watcher.

Day 1: database init + settings validation only.
Day 5 adds scheduler.start(); Day 9 adds position_watcher.start().
"""

from __future__ import annotations

import sys

from config.settings import settings
from core.logger import get_logger
from data.db import init_db

logger = get_logger(__name__)


def main() -> None:
    logger.info(
        "AI-Trading-System starting | ENV=%s | BROKER=%s | DRY_RUN=%s",
        settings.ENV,
        settings.BROKER,
        settings.DRY_RUN,
    )
    init_db()
    logger.info("Database ready at %s", settings.DB_PATH)
    # TODO Day 5: from watcher.scheduler import Scheduler; Scheduler().start()
    # TODO Day 9: from broker_client.position_watcher import PositionWatcher; PositionWatcher().start()
    logger.info("Startup complete. Sleeping until market phase activation.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutdown requested — exiting cleanly.")
        sys.exit(0)
    except Exception:
        logger.critical("Fatal startup error — see log for details.", exc_info=True)
        sys.exit(1)
