"""Tests for data.data_manager — unified DataManager facade."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.exceptions import DataError
from data.data_manager import DataManager

# ── Factories ─────────────────────────────────────────────────────────────────


def _dm(tmp_db: str, **mocks) -> DataManager:
    """Build a DataManager with all connectors replaced by mocks."""
    return DataManager(
        db_path=tmp_db,
        yfinance=mocks.get("yfinance", MagicMock()),
        alpaca=mocks.get("alpaca", MagicMock()),
        fred=mocks.get("fred", MagicMock()),
        edgar=mocks.get("edgar", MagicMock()),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_init_initialises_db(tmp_db: str) -> None:
    """DataManager.__init__ must not raise — DB already inited by fixture."""
    dm = _dm(tmp_db)
    assert dm is not None


def test_get_ohlcv_delegates_to_yfinance(
    tmp_db: str,
    sample_ohlcv_df: pd.DataFrame,
) -> None:
    mock_yf = MagicMock()
    mock_yf.get_ohlcv.return_value = sample_ohlcv_df
    dm = _dm(tmp_db, yfinance=mock_yf)

    result = dm.get_ohlcv("AAPL", "2024-01-02", "2024-01-04")

    mock_yf.get_ohlcv.assert_called_once_with("AAPL", "2024-01-02", "2024-01-04", "1d")
    assert len(result) == 3


def test_get_quote_delegates_to_alpaca(tmp_db: str) -> None:
    expected = {
        "ticker": "AAPL",
        "bid": 179.5,
        "ask": 179.6,
        "bid_size": 100,
        "ask_size": 200,
        "timestamp": None,
    }
    mock_alpaca = MagicMock()
    mock_alpaca.get_quote.return_value = expected
    dm = _dm(tmp_db, alpaca=mock_alpaca)

    result = dm.get_quote("AAPL")

    mock_alpaca.get_quote.assert_called_once_with("AAPL")
    assert result["ticker"] == "AAPL"


def test_get_macro_series_delegates_to_fred(tmp_db: str) -> None:
    expected = pd.Series([1.5, 1.6], index=pd.to_datetime(["2024-01-01", "2024-02-01"]))
    mock_fred = MagicMock()
    mock_fred.get_series.return_value = expected
    dm = _dm(tmp_db, fred=mock_fred)

    result = dm.get_macro_series("T10YIE", "2024-01-01")

    mock_fred.get_series.assert_called_once_with("T10YIE", "2024-01-01", None)
    assert len(result) == 2


def test_get_macro_latest_delegates_to_fred(tmp_db: str) -> None:
    mock_fred = MagicMock()
    mock_fred.get_latest.return_value = 4.25
    dm = _dm(tmp_db, fred=mock_fred)

    result = dm.get_macro_latest("DFF")

    mock_fred.get_latest.assert_called_once_with("DFF")
    assert result == pytest.approx(4.25)


def test_get_filings_delegates_to_edgar(tmp_db: str) -> None:
    expected = [
        {
            "accession_no": "0001-00",
            "form": "13F-HR",
            "filing_date": "2024-01-01",
            "description": "",
        }
    ]
    mock_edgar = MagicMock()
    mock_edgar.get_recent_filings.return_value = expected
    dm = _dm(tmp_db, edgar=mock_edgar)

    result = dm.get_filings("AAPL", form_type="13F-HR", n=5)

    mock_edgar.get_recent_filings.assert_called_once_with("AAPL", "13F-HR", 5)
    assert result[0]["accession_no"] == "0001-00"


def test_get_13f_holdings_delegates_to_edgar(tmp_db: str) -> None:
    mock_edgar = MagicMock()
    mock_edgar.get_13f_holdings.return_value = [{"name": "Vanguard"}]
    dm = _dm(tmp_db, edgar=mock_edgar)

    result = dm.get_13f_holdings("AAPL")

    mock_edgar.get_13f_holdings.assert_called_once_with("AAPL")
    assert result[0]["name"] == "Vanguard"


def test_get_insider_transactions_delegates_to_edgar(tmp_db: str) -> None:
    mock_edgar = MagicMock()
    mock_edgar.get_insider_transactions.return_value = []
    dm = _dm(tmp_db, edgar=mock_edgar)

    dm.get_insider_transactions("AAPL", n=10)

    mock_edgar.get_insider_transactions.assert_called_once_with("AAPL", 10)


def test_fundamentals_cache_roundtrip(tmp_db: str) -> None:
    dm = _dm(tmp_db)
    data = {"eps_growth": 15.0, "revenue_growth": 12.0, "sector": "Technology"}

    dm.set_fundamentals("AAPL", data)
    result = dm.get_fundamentals("AAPL")

    assert result == data


def test_get_fundamentals_returns_none_when_not_cached(tmp_db: str) -> None:
    dm = _dm(tmp_db)
    result = dm.get_fundamentals("XXXX")
    assert result is None


def test_set_fundamentals_overwrites_existing(tmp_db: str) -> None:
    dm = _dm(tmp_db)
    dm.set_fundamentals("AAPL", {"v": 1})
    dm.set_fundamentals("AAPL", {"v": 2})

    result = dm.get_fundamentals("AAPL")
    assert result == {"v": 2}


def test_get_ohlcv_propagates_data_error(tmp_db: str) -> None:
    mock_yf = MagicMock()
    mock_yf.get_ohlcv.side_effect = DataError("Feed down")
    dm = _dm(tmp_db, yfinance=mock_yf)

    with pytest.raises(DataError):
        dm.get_ohlcv("AAPL", "2024-01-01", "2024-12-31")
