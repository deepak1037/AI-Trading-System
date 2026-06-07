"""Tests for data.alpaca_connector — real-time quotes and historical bars."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.exceptions import DataError
from data.alpaca_connector import AlpacaConnector

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_quote() -> MagicMock:
    q = MagicMock()
    q.bid_price = 179.50
    q.ask_price = 179.55
    q.bid_size = 100
    q.ask_size = 200
    q.timestamp = datetime(2024, 1, 2, 15, 0, 0)
    return q


@pytest.fixture
def mock_bars_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [180.0, 182.0],
            "high": [185.0, 184.0],
            "low": [179.0, 181.0],
            "close": [184.0, 183.0],
            "volume": [50_000_000, 48_000_000],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_get_quote_missing_api_key_raises_data_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_API_KEY", "")
    connector = AlpacaConnector()
    with pytest.raises(DataError, match="ALPACA_API_KEY not configured"):
        connector.get_quote("AAPL")


def test_get_quote_returns_expected_structure(
    mock_quote: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_API_KEY", "test-key")
    monkeypatch.setattr(
        "data.alpaca_connector.settings.ALPACA_SECRET_KEY", "test-secret"
    )

    connector = AlpacaConnector()
    connector._get_quote_impl = MagicMock(
        return_value={
            "ticker": "AAPL",
            "bid": 179.50,
            "ask": 179.55,
            "bid_size": 100,
            "ask_size": 200,
            "timestamp": mock_quote.timestamp,
        }
    )

    result = connector.get_quote("AAPL")

    assert result["ticker"] == "AAPL"
    assert result["bid"] == pytest.approx(179.50)
    assert result["ask"] == pytest.approx(179.55)
    assert "timestamp" in result


def test_get_quote_raises_data_error_on_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_API_KEY", "key")
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_SECRET_KEY", "secret")

    connector = AlpacaConnector()
    connector._get_quote_impl = MagicMock(side_effect=DataError("Alpaca API down"))

    with pytest.raises(DataError):
        connector.get_quote("AAPL")


def test_fetch_quote_wraps_generic_exception_as_data_error(
    mock_quote: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_API_KEY", "key")
    monkeypatch.setattr("data.alpaca_connector.settings.ALPACA_SECRET_KEY", "secret")

    connector = AlpacaConnector()
    mock_client = MagicMock()
    mock_client.get_stock_latest_quote.side_effect = RuntimeError("timeout")
    connector._client = mock_client

    with pytest.raises(DataError, match="Alpaca latest quote failed"):
        connector._fetch_quote("AAPL")


def test_get_bars_returns_dataframe(mock_bars_df: pd.DataFrame) -> None:
    connector = AlpacaConnector()
    connector._get_bars_impl = MagicMock(return_value=mock_bars_df)

    result = connector.get_bars("AAPL", "2024-01-02", "2024-01-03")

    connector._get_bars_impl.assert_called_once()
    assert not result.empty
    assert "close" in result.columns


def test_fetch_bars_wraps_generic_exception_as_data_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = AlpacaConnector()
    mock_client = MagicMock()
    mock_client.get_stock_bars.side_effect = ConnectionError("network down")
    connector._client = mock_client

    with pytest.raises(DataError, match="Alpaca bars failed"):
        from alpaca.data.timeframe import TimeFrame

        connector._fetch_bars("AAPL", "2024-01-02", "2024-01-03", TimeFrame.Day)


def test_fetch_bars_returns_empty_df_when_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = AlpacaConnector()
    mock_result = MagicMock()
    mock_result.df = pd.DataFrame()
    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = mock_result
    connector._client = mock_client

    from alpaca.data.timeframe import TimeFrame

    result = connector._fetch_bars("ZZZZ", "2024-01-02", "2024-01-03", TimeFrame.Day)
    assert result.empty


@pytest.mark.integration
def test_integration_get_bars_aapl_live() -> None:
    """Live Alpaca call — requires ALPACA_API_KEY in environment."""
    connector = AlpacaConnector()
    from alpaca.data.timeframe import TimeFrame

    df = connector.get_bars("AAPL", "2024-01-02", "2024-01-05", TimeFrame.Day)
    assert not df.empty
