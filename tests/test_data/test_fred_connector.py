"""Tests for data.fred_connector — FRED macro series."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.exceptions import DataError
from data.fred_connector import FredConnector

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_connector_with_mock_fred(mock_series: pd.Series) -> FredConnector:
    connector = FredConnector()
    connector._series_impl = MagicMock(return_value=mock_series)
    return connector


def _sample_series() -> pd.Series:
    return pd.Series(
        [1.5, 1.6, 1.7, 1.8],
        index=pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]),
        name="T10YIE",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_get_series_returns_pandas_series() -> None:
    connector = _make_connector_with_mock_fred(_sample_series())
    result = connector.get_series("T10YIE", "2024-01-01")
    assert isinstance(result, pd.Series)
    assert len(result) == 4


def test_get_series_passes_args_to_impl() -> None:
    connector = FredConnector()
    connector._series_impl = MagicMock(return_value=_sample_series())

    connector.get_series("UNRATE", start="2023-01-01", end="2024-01-01")

    connector._series_impl.assert_called_once_with("UNRATE", "2023-01-01", "2024-01-01")


def test_get_latest_returns_last_value() -> None:
    connector = _make_connector_with_mock_fred(_sample_series())
    result = connector.get_latest("T10YIE")
    assert result == pytest.approx(1.8)


def test_get_latest_raises_data_error_for_empty_series() -> None:
    connector = _make_connector_with_mock_fred(pd.Series([], dtype=float))
    with pytest.raises(DataError, match="FRED series is empty"):
        connector.get_latest("EMPTY")


def test_get_series_propagates_data_error() -> None:
    connector = FredConnector()
    connector._series_impl = MagicMock(side_effect=DataError("FRED down"))
    with pytest.raises(DataError):
        connector.get_series("GDP")


def test_fetch_series_wraps_exception_as_data_error() -> None:
    connector = FredConnector()
    mock_fred = MagicMock()
    mock_fred.get_series.side_effect = ValueError("bad series id")
    connector._fred = mock_fred

    with pytest.raises(DataError, match="FRED series fetch failed"):
        connector._fetch_series("BAD_ID", None, None)


def test_fetch_series_no_api_key_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Missing FRED_API_KEY produces a warning, not an exception."""
    connector = FredConnector()
    connector._fred = None  # force lazy init

    import logging

    # Patch settings to empty key, then trigger lazy init via _get_client.
    with (
        patch("data.fred_connector.settings.FRED_API_KEY", ""),
        caplog.at_level(logging.WARNING, logger="data.fred_connector"),
        patch("data.fred_connector.Fred") as mock_cls,
    ):
        mock_cls.return_value = MagicMock()
        connector._get_client()
    assert any("FRED_API_KEY" in r.message for r in caplog.records)


def test_get_latest_skips_nan_values() -> None:
    series_with_nan = pd.Series(
        [1.5, float("nan"), 1.9],
        index=pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
    )
    connector = _make_connector_with_mock_fred(series_with_nan)
    result = connector.get_latest("T10YIE")
    assert result == pytest.approx(1.9)


@pytest.mark.integration
def test_integration_get_unrate_live() -> None:
    """Live FRED call — requires FRED_API_KEY or anonymous access."""
    connector = FredConnector()
    s = connector.get_series("UNRATE", start="2020-01-01", end="2020-06-01")
    assert not s.empty
