"""Unified DataManager — single access point for all market data.

All signal modules, scanner, and broker_client use this class rather than
importing connectors directly.  Dependency injection (passing connector
instances) keeps every module testable without network access.

Usage::

    from data.data_manager import DataManager
    dm = DataManager()
    ohlcv = dm.get_ohlcv("AAPL", "2024-01-01", "2024-12-31")
    quote = dm.get_quote("AAPL")
    gdp   = dm.get_macro_series("GDP")
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pandas as pd
from alpaca.data.timeframe import TimeFrame

from config.settings import settings
from core.logger import get_logger
from data.alpaca_connector import AlpacaConnector
from data.db import get_connection, init_db
from data.edgar_connector import EdgarConnector
from data.fred_connector import FredConnector
from data.yfinance_connector import YFinanceConnector

logger = get_logger(__name__)


class DataManager:
    """Coordinates all data connectors behind a single facade.

    Args:
        db_path: SQLite path; falls back to ``settings.DB_PATH``.
        yfinance: Injectable YFinanceConnector for testing.
        alpaca:   Injectable AlpacaConnector for testing.
        fred:     Injectable FredConnector for testing.
        edgar:    Injectable EdgarConnector for testing.
    """

    def __init__(
        self,
        db_path: str | None = None,
        yfinance: YFinanceConnector | None = None,
        alpaca: AlpacaConnector | None = None,
        fred: FredConnector | None = None,
        edgar: EdgarConnector | None = None,
    ) -> None:
        self._db = db_path or settings.DB_PATH
        init_db(self._db)

        self._yfinance = yfinance or YFinanceConnector(db_path=self._db)
        self._alpaca = alpaca or AlpacaConnector()
        self._fred = fred or FredConnector()
        self._edgar = edgar or EdgarConnector()

        logger.info("DataManager ready (db=%s)", self._db)

    # ── OHLCV ─────────────────────────────────────────────────

    def get_ohlcv(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame from yfinance (with SQLite cache)."""
        return self._yfinance.get_ohlcv(ticker, start, end, interval)

    # ── Real-time quotes ──────────────────────────────────────

    def get_quote(self, ticker: str) -> dict[str, Any]:
        """Return the latest NBBO quote from Alpaca."""
        return self._alpaca.get_quote(ticker)

    def get_bars(
        self,
        ticker: str,
        start: str,
        end: str,
        timeframe: TimeFrame = TimeFrame.Day,
    ) -> pd.DataFrame:
        """Return OHLCV bars from Alpaca."""
        return self._alpaca.get_bars(ticker, start, end, timeframe)

    # ── Macro data ────────────────────────────────────────────

    def get_macro_series(
        self,
        series_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.Series:
        """Return a FRED time series."""
        return self._fred.get_series(series_id, start, end)

    def get_macro_latest(self, series_id: str) -> float:
        """Return the latest value from a FRED series."""
        return self._fred.get_latest(series_id)

    # ── EDGAR ─────────────────────────────────────────────────

    def get_filings(
        self,
        ticker: str,
        form_type: str = "13F-HR",
        n: int = 5,
    ) -> list[dict[str, Any]]:
        """Return recent SEC filings from EDGAR."""
        return self._edgar.get_recent_filings(ticker, form_type, n)

    def get_13f_holdings(self, ticker: str) -> list[dict[str, Any]]:
        """Return latest 13F holdings from EDGAR."""
        return self._edgar.get_13f_holdings(ticker)

    def get_insider_transactions(
        self, ticker: str, n: int = 20
    ) -> list[dict[str, Any]]:
        """Return recent Form 4 insider transactions from EDGAR."""
        return self._edgar.get_insider_transactions(ticker, n)

    # ── Fundamentals cache ────────────────────────────────────

    def get_fundamentals(self, ticker: str) -> dict[str, Any] | None:
        """Return cached fundamentals for ``ticker``, or None if not cached."""
        with get_connection(self._db) as conn:
            row = conn.execute(
                "SELECT data_json FROM fundamentals_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if row is None:
            return None
        return cast(dict[str, Any], json.loads(row["data_json"]))

    def set_fundamentals(self, ticker: str, data: dict[str, Any]) -> None:
        """Upsert fundamentals data into the cache."""
        now = datetime.now(UTC).isoformat()
        with get_connection(self._db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fundamentals_cache (ticker, data_json, fetched_at) "
                "VALUES (?, ?, ?)",
                (ticker, json.dumps(data), now),
            )
        logger.debug("Fundamentals cache write: %s", ticker)


__all__ = ["DataManager"]
