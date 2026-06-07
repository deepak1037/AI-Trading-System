"""Reliable watchlist builder — curated liquid universe, yfinance-recovery aware.

The full 5-stage scanner downloads ~15,749 tickers and liquidity-filters them
through yfinance, which takes ~44 min AND exhausts yfinance's rate limit — so by
the time Stage 3 runs, every ``.info`` call returns empty and 0 stocks pass.

This script avoids that: it uses a curated, inherently-liquid universe (S&P 500 +
Nasdaq-100 from static CSVs — no yfinance needed for the list), waits for
yfinance to be healthy before each attempt, then runs Stages 3→4→5 and updates
the watchlist. It self-retries (with cooldown waits) until the watchlist is
populated or the time budget is exhausted.

Usage:
    python scripts/build_watchlist.py            # default budget ~3.5h
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.logger import get_logger  # noqa: E402

logger = get_logger("build_watchlist")

PROBE_TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "WMT", "KO", "PFE"]
COOLDOWN_SECONDS = 900          # wait between attempts when yfinance is blocked
MAX_ATTEMPTS = 10               # 10 * 15min ≈ 2.5h of patience
PROBE_PACE = 0.25               # pacing for the health probe
SCAN_PACE = 0.6                 # per-ticker pacing during the funnel (stay under yf limit)


# ── Curated liquid universe (no yfinance needed) ──────────────────────────────
def curated_universe() -> list[str]:
    import pandas as pd
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "AI-Trading-System/1.0"
    tickers: set[str] = set()

    sources = [
        ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
         "/main/data/constituents.csv", "Symbol"),
    ]
    for url, col in sources:
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(pd.io.common.StringIO(resp.text))
            tickers.update(df[col].astype(str).str.replace(".", "-", regex=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Universe source failed (%s): %s", url, exc)

    # Nasdaq-100 via Wikipedia (best-effort; S&P 500 alone is enough if it fails)
    try:
        import pandas as pd  # noqa: F811
        resp = session.get("https://en.wikipedia.org/wiki/Nasdaq-100", timeout=15)
        resp.raise_for_status()
        for tbl in pd.read_html(pd.io.common.StringIO(resp.text)):
            for c in tbl.columns:
                if str(c).lower() in ("ticker", "symbol"):
                    tickers.update(tbl[c].astype(str).str.replace(".", "-", regex=False))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Nasdaq-100 fetch skipped: %s", exc)

    clean = sorted(t for t in tickers if t and t[0].isalpha() and len(t) <= 5)
    logger.info("Curated universe: %d liquid tickers", len(clean))
    return clean


# ── yfinance health probe ─────────────────────────────────────────────────────
def yfinance_healthy() -> bool:
    """True if .info returns real fundamental fields (i.e. not rate-limited)."""
    import yfinance as yf

    ok = 0
    for t in PROBE_TICKERS:
        try:
            info = yf.Ticker(t).info or {}
            if info.get("trailingEps") is not None or info.get("revenueGrowth") is not None:
                ok += 1
        except Exception:  # noqa: BLE001
            pass
        time.sleep(PROBE_PACE)
    healthy = ok >= max(2, len(PROBE_TICKERS) // 2)
    logger.info("yfinance probe: %d/%d healthy -> %s", ok, len(PROBE_TICKERS), healthy)
    return healthy


# ── One full attempt: Stages 3→4 then watchlist (which runs Stage 5) ──────────
def run_pipeline(universe: list[str]) -> int:
    # Pace every per-ticker yfinance call in the scanner so the funnel doesn't
    # trip the rate limit and starve Stage 5 (the cause of empty watchlists).
    import config.settings as cs
    cs.settings.SCANNER_YF_PACE_SECONDS = SCAN_PACE

    from scanner.accumulation_screen import AccumulationScreen
    from scanner.fundamental_screen import FundamentalScreen
    from scanner.watchlist_manager import WatchlistManager

    logger.info("Stage 3: fundamental screen on %d tickers (paced %.2fs)", len(universe), SCAN_PACE)
    s3 = FundamentalScreen().screen(universe)
    logger.info("Stage 3 -> %d pass", len(s3))
    if not s3:
        return 0

    logger.info("Stage 4: accumulation screen on %d tickers", len(s3))
    s4 = AccumulationScreen().screen(s3)
    logger.info("Stage 4 -> %d pass", len(s4))

    # update_from_stage4 runs Stage 5 (technical) internally, scores survivors,
    # and adds those clearing the composite threshold. Don't let an over-strict
    # Stage 4 zero out the input.
    s4_input = s4 or s3
    logger.info("Watchlist update (runs Stage 5 internally) on %d tickers", len(s4_input))
    wm = WatchlistManager()
    wm.update_from_stage4(s4_input)
    active = len(wm.get_active())
    logger.info("Watchlist updated: %d active", active)
    return active


def main() -> int:
    universe = curated_universe()
    if not universe:
        logger.error("Could not build a universe — aborting.")
        return 1

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("=== Attempt %d/%d ===", attempt, MAX_ATTEMPTS)
        if not yfinance_healthy():
            logger.warning(
                "yfinance still rate-limited; cooling down %ds before retry.",
                COOLDOWN_SECONDS,
            )
            time.sleep(COOLDOWN_SECONDS)
            continue
        try:
            active = run_pipeline(universe)
        except Exception as exc:  # noqa: BLE001
            logger.error("Pipeline attempt failed: %s", exc)
            active = 0
        if active > 0:
            logger.info("SUCCESS: watchlist has %d active names.", active)
            return 0
        logger.warning(
            "Watchlist empty after attempt %d (likely yfinance throttled mid-run); "
            "cooling down %ds.", attempt, COOLDOWN_SECONDS,
        )
        time.sleep(COOLDOWN_SECONDS)

    logger.error("Exhausted %d attempts without a populated watchlist.", MAX_ATTEMPTS)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
