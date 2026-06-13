"""Tests for the Phase 3 earnings data connector + manual CSV + priority chain."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from broker_client.earnings.manual_input import ManualEarningsInput
from broker_client.earnings.models import EarningsEvent, EarningsIVHistory
from broker_client.earnings.moomoo_earnings import (
    EarningsDataProvider,
    MoomooEarningsConnector,
    _fmp_time,
    _num,
    _parse_any_date,
)

_MOOMOO_CSV = (
    "Ticker,Earnings Date,IV,Last IV Crush,Historical IV Crush,IV Rank,"
    "IV Percentile,Expected Move,Chg on Last Earnings,Chg on Historical Est,"
    "Forecast Revenue YoY,Forecast EPS YoY\n"
    "NVDA,2026-06-20,62.5%,28.0%,25.0%,72,80,±8.0%,+6.5%,7.2%,55.0%,40.0%\n"
    "AAPL,06/25/2026,30.0%,12.0%,10.0%,45,40,±4.0%,-2.0%,3.5%,8.0%,6.0%\n"
)


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "upcoming_earnings.csv"
    p.write_text(_MOOMOO_CSV)
    return str(p)


# ── ManualEarningsInput ─────────────────────────────────────────────────────────
def test_manual_load_parses_moomoo_csv(csv_file) -> None:
    events = ManualEarningsInput(csv_file).load()
    assert len(events) == 2
    nvda = next(e for e in events if e.ticker == "NVDA")
    assert nvda.earnings_date == date(2026, 6, 20)
    assert nvda.iv_current == 62.5
    assert nvda.iv_rank == 72
    assert nvda.expected_move == 8.0  # ± and % stripped
    assert nvda.last_earnings_move == 6.5
    assert nvda.forecast_revenue_yoy == 55.0
    assert nvda.source == "manual"


def test_manual_load_handles_alt_date_format(csv_file) -> None:
    events = ManualEarningsInput(csv_file).load()
    aapl = next(e for e in events if e.ticker == "AAPL")
    assert aapl.earnings_date == date(2026, 6, 25)  # 06/25/2026 parsed


def test_manual_load_missing_file_returns_empty() -> None:
    assert ManualEarningsInput("/nonexistent/path.csv").load() == []


def test_manual_load_skips_bad_rows(tmp_path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("Ticker,Earnings Date,IV\nGOOD,2026-07-01,40\n,,,\nNODATE,N/A,30\n")
    events = ManualEarningsInput(str(bad)).load()
    assert [e.ticker for e in events] == ["GOOD"]


def test_manual_exists(csv_file) -> None:
    assert ManualEarningsInput(csv_file).exists() is True
    assert ManualEarningsInput("/no/file.csv").exists() is False


def test_manual_na_values_degrade_to_none(tmp_path) -> None:
    p = tmp_path / "na.csv"
    p.write_text("Ticker,Earnings Date,IV,Forecast EPS YoY\nX,2026-07-01,--,N/A\n")
    events = ManualEarningsInput(str(p)).load()
    assert events[0].iv_current == 0.0           # required numeric → 0
    assert events[0].forecast_eps_yoy is None     # optional numeric → None


def test_manual_time_normalization(tmp_path) -> None:
    p = tmp_path / "t.csv"
    p.write_text(
        "Ticker,Earnings Date,Time\n"
        "A,2026-07-01,Before Market Open\n"
        "B,2026-07-01,after close\n"
        "C,2026-07-01,\n"
    )
    events = {e.ticker: e for e in ManualEarningsInput(str(p)).load()}
    assert events["A"].earnings_time == "BMO"
    assert events["B"].earnings_time == "AMC"
    assert events["C"].earnings_time == "AMC"  # default


# ── parsing helpers ───────────────────────────────────────────────────────────
def test_num_rejects_nan_and_sentinels() -> None:
    assert _num(None) is None
    assert _num("abc") is None
    assert _num(float("nan")) is None
    assert _num(1e15) is None
    assert _num("3.5") == 3.5


def test_parse_any_date_formats() -> None:
    assert _parse_any_date("2026-07-01") == date(2026, 7, 1)
    assert _parse_any_date("07/01/2026") == date(2026, 7, 1)
    assert _parse_any_date(date(2026, 7, 1)) == date(2026, 7, 1)
    assert _parse_any_date(None) is None
    assert _parse_any_date("garbage") is None


def test_fmp_time() -> None:
    assert _fmp_time("bmo") == "BMO"
    assert _fmp_time("after market close") == "AMC"
    assert _fmp_time(None) == "AMC"


# ── EarningsDataProvider priority chain ─────────────────────────────────────────
def test_provider_prefers_moomoo(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    ev = EarningsEvent(ticker="MMOO", earnings_date=date.today() + timedelta(days=3))
    monkeypatch.setattr(conn, "get_upcoming_earnings", lambda days=14: [ev])
    provider = EarningsDataProvider(connector=conn)
    out = provider.get_upcoming_earnings(14)
    assert [e.ticker for e in out] == ["MMOO"]


def test_provider_falls_back_to_csv(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "get_upcoming_earnings", lambda days=14: [])
    manual = ManualEarningsInput("/no/file.csv")
    csv_event = EarningsEvent(ticker="CSV", earnings_date=date.today() + timedelta(days=2))
    monkeypatch.setattr(manual, "load", lambda: [csv_event])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    out = provider.get_upcoming_earnings(14)
    assert [e.ticker for e in out] == ["CSV"]


def test_provider_window_filters_out_of_range(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "get_upcoming_earnings", lambda days=14: [])
    manual = ManualEarningsInput("/no/file.csv")
    near = EarningsEvent(ticker="NEAR", earnings_date=date.today() + timedelta(days=5))
    far = EarningsEvent(ticker="FAR", earnings_date=date.today() + timedelta(days=60))
    monkeypatch.setattr(manual, "load", lambda: [near, far])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    out = provider.get_upcoming_earnings(14)
    assert [e.ticker for e in out] == ["NEAR"]


def test_provider_returns_empty_when_no_source(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "get_upcoming_earnings", lambda days=14: [])
    manual = ManualEarningsInput("/no/file.csv")
    monkeypatch.setattr(manual, "load", lambda: [])
    from config.settings import settings

    monkeypatch.setattr(settings, "FMP_API_KEY", "")
    provider = EarningsDataProvider(connector=conn, manual=manual)
    monkeypatch.setattr(provider, "_yfinance_dates", lambda days: [])
    assert provider.get_upcoming_earnings(14) == []


def test_iv_history_empty_when_unavailable(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(
        conn, "get_earnings_iv_history",
        lambda t: EarningsIVHistory(ticker=t, quarters=[]),
    )
    provider = EarningsDataProvider(connector=conn)
    hist = provider.get_iv_history("ABC")
    assert hist.ticker == "ABC"
    assert hist.count == 0


def test_watchlist_tickers_empty_without_db(monkeypatch) -> None:
    # No watchlist rows → empty list, and get_upcoming_earnings returns [] early.
    monkeypatch.setattr(MoomooEarningsConnector, "_watchlist_tickers", staticmethod(lambda: []))
    assert MoomooEarningsConnector().get_upcoming_earnings(14) == []
