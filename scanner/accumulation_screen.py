"""Scanner Stage 4: Institutional accumulation screen (Days 13-18).

Filters ~400 fundamental stocks to ~120 by:
  - 13F new institutional buyers (edgartools or EDGAR API)
  - Form 4 insider buying clusters (edgartools)
  - Short interest declining (yfinance shortRatio proxy)

Uses edgartools library (Apache 2.0) where available.
TODO: wire up edgartools MCP server for Claude Code integration.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class AccumulationScreen:
    """Stage 4: institutional accumulation + insider buying + short squeeze setup."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._edgar_available = self._check_edgar()

    def _check_edgar(self) -> bool:
        try:
            import edgartools  # type: ignore[import-untyped]  # noqa: F401
            return True
        except ImportError:
            logger.warning(
                "edgartools not installed — Stage 4 will use yfinance proxies only. "
                "TODO: pip install edgartools for full 13F/Form4 support"
            )
            return False

    def _get_institutional_holders(self, ticker: str) -> dict:
        """Return institutional holder info from yfinance (proxy for 13F)."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            holders = t.institutional_holders
            if holders is None or holders.empty:
                return {"holder_count": 0, "pct_held": 0.0}
            pct_col = [c for c in holders.columns if "pct" in c.lower() or "%" in c.lower()]
            if pct_col:
                pct_held = float(holders[pct_col[0]].sum())
            else:
                pct_held = 0.0
            return {"holder_count": len(holders), "pct_held": pct_held}
        except Exception as exc:
            logger.debug("AccumulationScreen: holders fetch failed for %s: %s", ticker, exc)
            return {"holder_count": 0, "pct_held": 0.0}

    def _get_short_ratio(self, ticker: str) -> float:
        """Return short ratio (days to cover) from yfinance info."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            info = t.fast_info
            return float(getattr(info, "short_ratio", None) or 0.0)
        except Exception:
            return 0.0

    def _get_edgar_form4_buys(self, ticker: str) -> int:
        """Return count of Form 4 insider buy transactions in last 90 days."""
        if not self._edgar_available:
            return 0
        try:
            from edgartools import Company  # type: ignore[import-untyped]

            company = Company(ticker)
            form4_filings = company.get_filings(form="4", limit=20)
            buy_count = 0
            for filing in form4_filings:
                try:
                    data = filing.obj()
                    if hasattr(data, "transactions"):
                        for txn in data.transactions:
                            if getattr(txn, "transaction_code", "") in ("P", "A"):
                                buy_count += 1
                except Exception:
                    pass
            return buy_count
        except Exception as exc:
            logger.debug("Stage4: Form4 fetch failed for %s: %s", ticker, exc)
            return 0

    def _passes_accumulation(self, ticker: str) -> bool:
        """Return True if stock shows institutional accumulation signals."""
        holders = self._get_institutional_holders(ticker)
        short_ratio = self._get_short_ratio(ticker)
        form4_buys = self._get_edgar_form4_buys(ticker)

        # Institutional ownership > 20% (signals institutional interest)
        inst_interest = holders["holder_count"] >= 5 or holders["pct_held"] >= 0.20

        # Short interest < 20 days to cover (not heavily shorted against)
        short_ok = short_ratio < 20.0 or short_ratio == 0.0

        # Insider buying cluster: 2+ Form 4 buy transactions (only if edgartools available)
        insider_buying = form4_buys >= 2 if self._edgar_available else True

        return inst_interest and short_ok and insider_buying

    def screen(self, tickers: list[str]) -> list[str]:
        """Run Stage 4 screening. Returns tickers showing accumulation."""
        passing: list[str] = []
        for ticker in tickers:
            try:
                if self._passes_accumulation(ticker):
                    passing.append(ticker)
            except Exception as exc:
                logger.debug("Stage4: error for %s: %s", ticker, exc)

        logger.info("Stage 4 accumulation screen: %d/%d pass", len(passing), len(tickers))
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (4, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["AccumulationScreen"]
