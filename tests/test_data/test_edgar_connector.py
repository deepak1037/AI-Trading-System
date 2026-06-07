"""Tests for data.edgar_connector — SEC EDGAR filings via edgartools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.exceptions import DataError
from data.edgar_connector import EdgarConnector

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fake_filing(accession: str = "0001234567-24-000001") -> MagicMock:
    f = MagicMock()
    f.accession_no = accession
    f.filing_date = "2024-01-15"
    f.form = "13F-HR"
    f.description = "13F Holdings Report"
    return f


def _connector_with_mock_filings(filings: list) -> EdgarConnector:
    connector = EdgarConnector()
    connector._get_filings_impl = MagicMock(
        return_value=[
            {
                "accession_no": f.accession_no,
                "filing_date": f.filing_date,
                "form": f.form,
                "description": f.description,
            }
            for f in filings
        ]
    )
    return connector


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_get_recent_filings_returns_list() -> None:
    filings = [_fake_filing("0001-01"), _fake_filing("0001-02")]
    connector = _connector_with_mock_filings(filings)

    result = connector.get_recent_filings("AAPL", form_type="13F-HR", n=2)

    assert len(result) == 2
    assert result[0]["accession_no"] == "0001-01"
    assert result[0]["form"] == "13F-HR"


def test_get_recent_filings_delegates_correctly() -> None:
    connector = EdgarConnector()
    connector._get_filings_impl = MagicMock(return_value=[])

    connector.get_recent_filings("AAPL", form_type="4", n=10)

    connector._get_filings_impl.assert_called_once_with("AAPL", "4", 10)


def test_get_recent_filings_propagates_data_error() -> None:
    connector = EdgarConnector()
    connector._get_filings_impl = MagicMock(side_effect=DataError("EDGAR down"))

    with pytest.raises(DataError):
        connector.get_recent_filings("AAPL")


def test_fetch_filings_wraps_company_lookup_failure() -> None:
    connector = EdgarConnector()
    with (
        patch("data.edgar_connector.Company", side_effect=ValueError("not found")),
        pytest.raises(DataError, match="EDGAR company lookup failed"),
    ):
        connector._fetch_filings("ZZZZZZZ", "13F-HR", 5)


def test_fetch_filings_wraps_api_exception() -> None:
    connector = EdgarConnector()
    mock_company = MagicMock()
    mock_company.get_filings.side_effect = RuntimeError("network error")

    with (
        patch("data.edgar_connector.Company", return_value=mock_company),
        pytest.raises(DataError, match="EDGAR filings fetch failed"),
    ):
        connector._fetch_filings("AAPL", "13F-HR", 5)


def test_get_13f_holdings_delegates_to_holdings_impl() -> None:
    connector = EdgarConnector()
    connector._get_holdings_impl = MagicMock(return_value=[{"name": "Test Fund"}])

    result = connector.get_13f_holdings("AAPL")

    connector._get_holdings_impl.assert_called_once_with("AAPL")
    assert result[0]["name"] == "Test Fund"


def test_fetch_holdings_returns_empty_list_when_no_holdings() -> None:
    connector = EdgarConnector()
    mock_company = MagicMock()
    mock_filing = MagicMock()
    mock_13f = MagicMock()
    mock_13f.holdings = pd.DataFrame()  # empty
    mock_filing.obj.return_value = mock_13f
    mock_company.get_filings.return_value.latest.return_value = mock_filing

    with patch("data.edgar_connector.Company", return_value=mock_company):
        result = connector._fetch_holdings("AAPL")

    assert result == []


def test_get_insider_transactions_uses_form_4() -> None:
    connector = EdgarConnector()
    connector._get_filings_impl = MagicMock(return_value=[])

    connector.get_insider_transactions("AAPL", n=5)

    connector._get_filings_impl.assert_called_once_with("AAPL", "4", 5)


def test_get_recent_filings_empty_result_ok() -> None:
    connector = EdgarConnector()
    connector._get_filings_impl = MagicMock(return_value=[])
    result = connector.get_recent_filings("AAPL", form_type="SC 13G", n=3)
    assert result == []


@pytest.mark.integration
def test_integration_aapl_13f_live() -> None:
    """Live EDGAR call — no API key required, but needs network access."""
    connector = EdgarConnector()
    result = connector.get_recent_filings("AAPL", form_type="13F-HR", n=2)
    assert isinstance(result, list)
