"""Scanner funnel orchestration — merge per-stage data for real scoring.

Stages 3 (fundamental) and 4 (accumulation) each return per-stock dicts. This
merges them by ticker into the ``stage4_data`` structure that
``WatchlistManager.update_from_stage4`` expects, so each watchlist name is
scored from its own data instead of flat constants.

Stage 5 (technical) is run inside ``update_from_stage4`` and merged there.
"""

from __future__ import annotations

from scanner.accumulation_screen import AccumulationScreen
from scanner.fundamental_screen import FundamentalScreen
from core.logger import get_logger

logger = get_logger(__name__)


def run_funnel(universe: list[str]) -> dict[str, dict]:
    """Run Stages 3→4 and return merged per-stock data keyed by ticker.

    Returns ``{ticker: {ticker, eps_accelerating, rev_reaccelerating,
    est_revisions_up, holder_count, pct_held, form4_buys, short_ratio,
    short_interest_pct}}`` for names surviving both stages.
    """
    s3 = FundamentalScreen().screen(universe)
    s3_tickers = [d["ticker"] for d in s3]
    logger.info("Funnel: Stage 3 -> %d", len(s3_tickers))

    s4 = AccumulationScreen().screen(s3_tickers)
    logger.info("Funnel: Stage 4 -> %d", len(s4))

    fundamentals = {d["ticker"]: dict(d) for d in s3}
    merged: dict[str, dict] = {}
    for d in s4:
        ticker = d["ticker"]
        row = dict(fundamentals.get(ticker, {"ticker": ticker}))
        row.update(d)
        merged[ticker] = row
    return merged


__all__ = ["run_funnel"]
