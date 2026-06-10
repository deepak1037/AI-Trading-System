"""Tests for signals/bls_connector.py and MacroEngine's live BLS CPI path.

Covers: YoY computation from index levels, surprise-sigma derivation, the
BLS→FRED fallback, and the API-failure/edge paths. All BLS HTTP calls are
mocked — no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import DataError, RateLimitError, SignalError
from signals.bls_connector import BLSConnector, BLSObservation
from signals.macro_engine import MacroEngine, expected_cpi_period


# ---------------------------------------------------------------------------
# Helpers: build a fake BLS v2 JSON response. The API returns newest-first.
# ---------------------------------------------------------------------------
def _bls_response(series_id: str, monthly: list[tuple[int, int, float]]) -> dict:
    """monthly = list of (year, month, index_value), any order."""
    data = [
        {"year": str(y), "period": f"M{m:02d}", "periodName": "X", "value": str(v)}
        for (y, m, v) in monthly
    ]
    # API delivers newest-first; emulate that ordering by year/month desc.
    data.sort(key=lambda r: (int(r["year"]), int(r["period"][1:])), reverse=True)
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": series_id, "data": data}]},
    }


def _flat_series(base: float, yoy_pct: float, year: int) -> list[tuple[int, int, float]]:
    """3 years of monthly data where each month is exactly yoy_pct above prior year."""
    out: list[tuple[int, int, float]] = []
    for y in (year - 2, year - 1, year):
        factor = (1 + yoy_pct / 100.0) ** (y - (year - 2))
        for m in range(1, 13):
            out.append((y, m, round(base * factor, 3)))
    return out


@patch("requests.post")
def test_latest_yoy_computes_percentage(mock_post) -> None:
    """A clean 4% YoY index should yield yoy_pct≈4.0, not the index level."""
    series = _flat_series(base=300.0, yoy_pct=4.0, year=2026)
    resp = MagicMock()
    resp.json.return_value = _bls_response("CUUR0000SA0", series)
    resp.raise_for_status.return_value = None
    mock_post.return_value = resp

    reading = BLSConnector().latest_yoy("CUUR0000SA0", current_year=2026)

    assert reading.period_label == "2026-12"
    assert reading.yoy_pct == pytest.approx(4.0, abs=0.01)
    assert reading.index_value > 100  # it's an index level, sanity check
    assert reading.yoy_std > 0


@patch("requests.post")
def test_fetch_series_skips_annual_average(mock_post) -> None:
    """M13 (annual average) rows must be filtered out."""
    raw = _bls_response("CUUR0000SA0", [(2026, 1, 310.0), (2026, 2, 311.0)])
    raw["Results"]["series"][0]["data"].append(
        {"year": "2026", "period": "M13", "periodName": "Annual", "value": "999"}
    )
    resp = MagicMock()
    resp.json.return_value = raw
    resp.raise_for_status.return_value = None
    mock_post.return_value = resp

    obs = BLSConnector().fetch_series("CUUR0000SA0", 2026, 2026)

    assert all(o.period != "M13" for o in obs)
    assert {o.value for o in obs} == {310.0, 311.0}


@patch("requests.post")
def test_api_error_status_raises_dataerror(mock_post) -> None:
    resp = MagicMock()
    resp.json.return_value = {"status": "REQUEST_NOT_PROCESSED", "message": ["bad key"]}
    resp.raise_for_status.return_value = None
    mock_post.return_value = resp

    with pytest.raises(DataError, match="BLS API error"):
        BLSConnector().fetch_series("CUUR0000SA0", 2026, 2026)


@patch("requests.post")
def test_missing_prior_year_raises(mock_post) -> None:
    """Only one year of data → no prior-year base → DataError."""
    one_year = [(2026, m, 300.0 + m) for m in range(1, 13)]
    resp = MagicMock()
    resp.json.return_value = _bls_response("CUUR0000SA0", one_year)
    resp.raise_for_status.return_value = None
    mock_post.return_value = resp

    with pytest.raises(DataError, match="no prior-year value"):
        BLSConnector().latest_yoy("CUUR0000SA0", current_year=2026)


def test_observation_helpers() -> None:
    o = BLSObservation(year=2026, period="M05", value=313.5)
    assert o.month == 5
    assert o.label == "2026-05"


# ---------------------------------------------------------------------------
# MacroEngine.score_release_bls — surprise direction & fallback
# ---------------------------------------------------------------------------
def _patch_reading(yoy_pct: float, std: float = 0.2, label: str = "2026-05"):
    """Patch BLSConnector.latest_yoy to return a fixed reading."""
    from signals.bls_connector import BLSReading

    reading = BLSReading(
        series_id="CUUR0000SA0",
        period_label=label,
        index_value=313.5,
        yoy_pct=yoy_pct,
        yoy_std=std,
    )
    return patch.object(BLSConnector, "latest_yoy", return_value=reading)


def test_hot_cpi_is_bearish() -> None:
    """Actual YoY well above consensus = hotter inflation = short/strong_short."""
    eng = MacroEngine()
    # consensus 4.2, actual 4.8, std 0.2 → z=+3.0 → strong inflation surprise → strong_short
    with _patch_reading(yoy_pct=4.8):
        sig = eng.score_release_bls("CPI", consensus=4.2, current_year=2026)
    assert sig.direction == "strong_short"
    assert sig.source == "macro"
    assert sig.metadata["source_api"] == "BLS"
    assert sig.metadata["actual_yoy"] == 4.8
    assert sig.metadata["period"] == "2026-05"


def test_cool_cpi_is_bullish() -> None:
    """Actual YoY below consensus = cooler inflation = long."""
    eng = MacroEngine()
    # consensus 4.2, actual 3.9, std 0.2 → z=-1.5 → cooler → long
    with _patch_reading(yoy_pct=3.9):
        sig = eng.score_release_bls("CPI", consensus=4.2, current_year=2026)
    assert sig.direction == "long"


def test_inline_consensus_neutral() -> None:
    """Actual == consensus → z≈0 → neutral."""
    eng = MacroEngine()
    with _patch_reading(yoy_pct=4.2):
        sig = eng.score_release_bls("CPI", consensus=4.2, current_year=2026)
    assert sig.direction == "neutral"


def test_unknown_release_raises() -> None:
    with pytest.raises(SignalError):
        MacroEngine().score_release_bls("NFP")


def test_score_cpi_live_falls_back_to_fred_yoy() -> None:
    """When BLS raises, score_cpi_live falls back to the YoY-correct FRED path."""
    eng = MacroEngine()
    with patch.object(
        BLSConnector, "latest_yoy", side_effect=DataError("BLS down")
    ), patch.object(eng, "_score_release_fred_yoy") as mock_fred:
        mock_fred.return_value = MagicMock(direction="short", source="macro")
        eng.score_cpi_live("CPI", current_year=2026)
    mock_fred.assert_called_once_with("CPI")


# ---------------------------------------------------------------------------
# Bug 1 — rate-limit handling + 5-min TTL cache
# ---------------------------------------------------------------------------
def test_rate_limit_not_retried() -> None:
    """A 429 raises RateLimitError immediately — NOT retried as a DataError."""
    resp = MagicMock()
    resp.status_code = 429
    with patch("requests.post", return_value=resp) as mock_post, \
         pytest.raises(RateLimitError):
        BLSConnector().fetch_series("CUUR0000SA0", 2025, 2026)
    # Only one POST — proves no retry loop ran on the throttle.
    assert mock_post.call_count == 1


def test_rate_limit_in_body_message() -> None:
    """A 200 response whose body says 'threshold' is treated as a throttle."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "status": "REQUEST_NOT_PROCESSED",
        "message": ["daily threshold for number of requests exceeded"],
    }
    with patch("requests.post", return_value=resp), \
         pytest.raises(RateLimitError):
        BLSConnector().fetch_series("CUUR0000SA0", 2025, 2026)


def test_bls_reading_cached_within_ttl() -> None:
    """The macro tick polls BLS at most once per TTL (quota protection)."""
    eng = MacroEngine()
    with _patch_reading(yoy_pct=4.0) as mocked:
        eng._get_bls_reading("CUUR0000SA0", 2026)
        eng._get_bls_reading("CUUR0000SA0", 2026)
        eng._get_bls_reading("CUUR0000SA0", 2026)
    assert mocked.call_count == 1  # cached after first fetch


def test_rate_limit_serves_stale_cache() -> None:
    """On a throttle with a cached reading, the cached value is served (no raise)."""
    eng = MacroEngine()
    from signals.bls_connector import BLSReading

    good = BLSReading("CUUR0000SA0", "2026-05", 313.5, 4.0, 0.2)
    with patch.object(BLSConnector, "latest_yoy", return_value=good):
        eng._get_bls_reading("CUUR0000SA0", 2026)          # populate cache
    eng._bls_cache["CUUR0000SA0"] = (0.0, good)            # force "stale" (ts=0)
    with patch.object(BLSConnector, "latest_yoy", side_effect=RateLimitError("429")):
        served = eng._get_bls_reading("CUUR0000SA0", 2026)
    assert served is good


def test_rate_limit_raises_when_no_cache() -> None:
    """Throttle with no cached reading propagates so caller can FRED-fallback."""
    eng = MacroEngine()
    with patch.object(BLSConnector, "latest_yoy", side_effect=RateLimitError("429")), \
         pytest.raises(RateLimitError):
        eng._get_bls_reading("CUUR0000SA0", 2026)


# ---------------------------------------------------------------------------
# Release-gate parsing + fusion-level "don't fire on stale month" behaviour
# ---------------------------------------------------------------------------
def test_expected_cpi_period_parsing() -> None:
    assert expected_cpi_period("May 2026") == "2026-05"
    assert expected_cpi_period("December 2025") == "2025-12"
    assert expected_cpi_period("garbage") is None


def test_fusion_skips_stale_month_before_release() -> None:
    """Before the print, BLS serves the prior month → no macro signal fires."""
    from signals.signal_fusion import SignalFusion

    # Latest published period (2026-04) is OLDER than expected release (2026-05).
    with _patch_reading(yoy_pct=3.81, label="2026-04"), patch(
        "config.settings.settings.CPI_RELEASE_LABEL", "May 2026"
    ):
        result = SignalFusion()._collect_macro_cpi()
    assert result is None


def test_fusion_fires_once_month_published() -> None:
    """Once BLS serves the expected month, the macro signal is emitted."""
    from signals.signal_fusion import SignalFusion

    with _patch_reading(yoy_pct=4.8, label="2026-05"), patch(
        "config.settings.settings.CPI_RELEASE_LABEL", "May 2026"
    ):
        result = SignalFusion()._collect_macro_cpi()
    assert result is not None
    assert result.source == "macro"
    assert result.metadata["period"] == "2026-05"
