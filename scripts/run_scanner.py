"""Full 5-stage scanner — universe → funnel → scored watchlist.

Stages 1-2 download and liquidity-filter the universe, then the shared funnel
(Stages 3-4) produces per-stock data, and ``update_from_stage4`` runs Stage 5
and writes the scored watchlist.

NOTE: the full 15,749-ticker universe takes ~40 min in Stage 2 and tends to
exhaust yfinance's rate limit, starving the later stages. For a reliable run use
``python scripts/build_watchlist.py`` (curated universe + pacing + recovery).
Set SCANNER_YF_PACE_SECONDS in .env to pace this run too.

Usage:
    python scripts/run_scanner.py
    make scanner
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scanner.pipeline import run_funnel  # noqa: E402
from scanner.universe import UniverseDownloader  # noqa: E402
from scanner.watchlist_manager import WatchlistManager  # noqa: E402


def main() -> int:
    print("Stage 1-2: Universe + liquidity filter")
    universe = UniverseDownloader().run()
    print(f"  -> {len(universe)} liquid stocks")
    if not universe:
        print("No liquid universe — aborting.")
        return 1

    print("Stages 3-4: Fundamental + accumulation funnel")
    stage4_data = run_funnel(universe)
    print(f"  -> {len(stage4_data)} pass to scoring")

    print("Stage 5 + scoring: updating watchlist")
    wm = WatchlistManager()
    wm.update_from_stage4(stage4_data)
    active = wm.get_active()
    print(f"Done. {len(active)} stocks on watchlist.")
    if active:
        top = ", ".join(f"{e['ticker']}({e['composite_score']})" for e in active[:10])
        print(f"  Top by score: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
