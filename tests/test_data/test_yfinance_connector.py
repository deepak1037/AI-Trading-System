"""Tests for data.yfinance_connector — OHLCV fetching + SQLite caching."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.exceptions import DataError
from data.db import get_connection
from data.yfinance_connector import YFinanceConnector

# ── Helpers ───────────────────────────────────────────────────────────────────


def _insert_cache_rows(db_path: str, ticker: str, df: pd.DataFrame) -> None:
    rows = [
        (ticker, str(idx), r["open"], r["high"], r["low"], r["close"], int(r["volume"]))
        for idx, r in df.iterrows()
    ]
    with get_connection(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv_cache "
            "(ticker, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_get_ohlcv_calls_fetch_raw_when_cache_is_empty(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    # Patch the retry-wrapped callable to return sample data without a real API call.
    connector._fetch_with_retry = MagicMock(return_value=sample_ohlcv_df)

    result = connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")

    connector._fetch_with_retry.assert_called_once_with(
        "AAPL", "2024-01-02", "2024-01-04", "1d"
    )
    assert len(result) == 3


def test_get_ohlcv_returns_cache_without_api_call(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    _insert_cache_rows(tmp_db, "AAPL", sample_ohlcv_df)
    connector._fetch_with_retry = MagicMock()

    result = connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")

    connector._fetch_with_retry.assert_not_called()
    assert not result.empty
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]


def test_get_ohlcv_populates_cache_after_fetch(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    connector._fetch_with_retry = MagicMock(return_value=sample_ohlcv_df)

    connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")

    with get_connection(tmp_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE ticker = 'AAPL'"
        ).fetchone()[0]
    assert count == 3


def test_get_ohlcv_intraday_skips_cache(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    _insert_cache_rows(tmp_db, "AAPL", sample_ohlcv_df)
    connector._fetch_with_retry = MagicMock(return_value=sample_ohlcv_df)

    connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04", interval="1h")

    # Cache was populated but interval != 1d so it should not be used.
    connector._fetch_with_retry.assert_called_once()


def test_get_ohlcv_propagates_data_error(tmp_db: str) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    connector._fetch_with_retry = MagicMock(side_effect=DataError("API down"))

    with pytest.raises(DataError):
        connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")


def test_fetch_raw_normalises_column_names(tmp_db: str) -> None:
    """_fetch_raw must return lowercase columns regardless of yfinance capitalisation."""
    connector = YFinanceConnector(db_path=tmp_db)
    raw_df = pd.DataFrame(
        {
            "Open": [180.0],
            "High": [185.0],
            "Low": [179.0],
            "Close": [184.0],
            "Volume": [50_000_000],
            "Dividends": [0.0],
            "Stock Splits": [0.0],
        },
        index=[pd.Timestamp("2024-01-02", tz="America/New_York")],
    )
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = raw_df

    with patch("data.yfinance_connector.yf.Ticker", return_value=mock_ticker):
        result = connector._fetch_raw("AAPL", "2024-01-02", "2024-01-02")

    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.index[0] == date(2024, 1, 2)


def test_fetch_raw_raises_data_error_on_exception(tmp_db: str) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = ConnectionError("network down")

    with (
        patch("data.yfinance_connector.yf.Ticker", return_value=mock_ticker),
        pytest.raises(DataError, match="yfinance API error"),
    ):
        connector._fetch_raw("AAPL", "2024-01-02", "2024-01-04")


def test_fetch_raw_returns_empty_df_for_no_data(tmp_db: str) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()

    with patch("data.yfinance_connector.yf.Ticker", return_value=mock_ticker):
        result = connector._fetch_raw("ZZZZ", "2024-01-01", "2024-01-31")

    assert result.empty


def test_get_ohlcv_second_call_uses_cache(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    connector = YFinanceConnector(db_path=tmp_db)
    connector._fetch_with_retry = MagicMock(return_value=sample_ohlcv_df)

    connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")
    connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")

    # Only one real API call; second served from cache.
    assert connector._fetch_with_retry.call_count == 1


@pytest.mark.integration
def test_integration_fetch_aapl_live() -> None:
    """Live yfinance call — run with: pytest -m integration."""
    connector = YFinanceConnector()
    df = connector.get_ohlcv("AAPL", "2024-01-02", "2024-01-05")
    assert not df.empty
    assert "close" in df.columns
