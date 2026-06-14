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
    _normalize_iv,
    _num,
    _parse_any_date,
)


class _FakeCtx:
    """Stand-in for a Moomoo OpenQuoteContext for the IV-enrichment path."""

    def __init__(self, ivs, spot=150.0, hv=20.0) -> None:
        self._ivs = ivs
        self._spot = spot
        self._hv = hv

    def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        df = pd.DataFrame(
            {
                "timestamp": list(range(len(self._ivs))),
                "implied_volatility": self._ivs,
                "history_volatility": [self._hv] * len(self._ivs),
            }
        )
        return ft.RET_OK, df

    def get_market_snapshot(self, codes):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        return ft.RET_OK, pd.DataFrame([{"last_price": self._spot}])

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


def test_epoch_to_date() -> None:
    from broker_client.earnings.moomoo_earnings import _epoch_to_date

    assert _epoch_to_date(1_700_000_000).year == 2023
    assert _epoch_to_date(0) is None
    assert _epoch_to_date(None) is None


def test_quarter_from_series_computes_crush() -> None:
    ed = date(2026, 3, 10)
    series = [(ed - timedelta(days=10), 30.0), (ed - timedelta(days=1), 60.0),
              (ed + timedelta(days=2), 35.0)]
    q = MoomooEarningsConnector._quarter_from_series(ed, series, actual_move=-8.0)
    assert q is not None
    assert q.iv_before == 60.0
    assert q.iv_after == 35.0
    assert q.iv_crush == pytest.approx((60 - 35) / 60 * 100, abs=0.1)  # ~41.7%
    assert q.actual_move_close == -8.0
    assert q.expected_move > 0


def test_quarter_from_series_skips_without_after_sample() -> None:
    ed = date(2026, 3, 10)
    series = [(ed - timedelta(days=1), 60.0)]  # no post-earnings sample
    assert MoomooEarningsConnector._quarter_from_series(ed, series, None) is None


def test_iv_series_parses_dataframe(monkeypatch) -> None:
    import pandas as pd

    class _Ctx:
        def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
            import moomoo as ft  # type: ignore[import-untyped]

            df = pd.DataFrame({
                "timestamp_str": ["2026-03-12", "2026-03-09"],
                "implied_volatility": [35.0, 60.0],
            })
            return ft.RET_OK, df

    conn = MoomooEarningsConnector()
    conn._ctx = _Ctx()
    series = conn._iv_series("NVDA")
    assert series == [(date(2026, 3, 9), 60.0), (date(2026, 3, 12), 35.0)]  # sorted oldest-first


def test_get_earnings_iv_history_orchestration(monkeypatch) -> None:
    import broker_client.earnings.moomoo_earnings as mod

    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    ed = date.today() - timedelta(days=90)
    monkeypatch.setattr(
        conn, "_iv_series",
        lambda t: [(ed - timedelta(days=1), 60.0), (ed + timedelta(days=2), 35.0)],
    )
    monkeypatch.setattr(mod, "_past_earnings_dates", lambda t, **kw: [ed])
    monkeypatch.setattr(mod, "_earnings_moves", lambda t, dates: {ed: -8.0})

    hist = conn.get_earnings_iv_history("NVDA")
    assert hist.count == 1
    q = hist.quarters[0]
    assert q.iv_crush == pytest.approx(41.67, abs=0.1)
    assert q.actual_move_close == -8.0


def test_get_earnings_iv_history_empty_when_opend_down(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: False)
    assert conn.get_earnings_iv_history("NVDA").count == 0


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


def test_connector_no_native_earnings_calendar() -> None:
    # Moomoo has no earnings-calendar endpoint → connector.get_upcoming_earnings is [].
    assert MoomooEarningsConnector().get_upcoming_earnings(14) == []


# ── Moomoo IV enrichment ────────────────────────────────────────────────────────
def test_normalize_iv() -> None:
    assert _normalize_iv(45.2) == 45.2      # already a percent
    assert _normalize_iv(0.45) == 45.0      # fraction → percent
    assert _normalize_iv(0) is None
    assert _normalize_iv(None) is None


def test_get_underlying_iv_computes_rank_and_percentile(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtx(ivs=[20, 30, 40, 50, 45])  # latest = 45
    iv = conn.get_underlying_iv("NVDA")
    assert iv is not None
    assert iv["iv_current"] == 45.0
    assert iv["iv_rank"] == 83            # (45-20)/(50-20)*100
    assert iv["iv_percentile"] == 80      # 4 of 5 values <= 45
    assert iv["samples"] == 5


def test_get_underlying_iv_empty_series_returns_none(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtx(ivs=[])
    assert conn.get_underlying_iv("X") is None


def test_option_volatility_uses_year_period_not_quarter() -> None:
    """Regression: must send OptionVolatilityTimePeriodType_Year (5), not 3."""
    from broker_client.earnings.moomoo_earnings import _vol_year_period

    assert _vol_year_period() == 5            # Year, not RangePeriod.ONE_YEAR (3=Quarter)

    class _RecCtx:
        def __init__(self) -> None:
            self.period = "unset"

        def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
            import moomoo as ft  # type: ignore[import-untyped]
            import pandas as pd

            self.period = query_time_period
            df = pd.DataFrame(
                {"timestamp": [1, 2], "implied_volatility": [30.0, 45.0],
                 "history_volatility": [20.0, 20.0]}
            )
            return ft.RET_OK, df

    conn = MoomooEarningsConnector()
    rec = _RecCtx()
    conn._ctx = rec
    iv = conn.get_underlying_iv("NVDA")
    assert rec.period == 5                    # the corrected enum value
    assert iv is not None and iv["iv_source"] == "option_volatility"


def test_get_underlying_iv_returns_none_on_ret_error(monkeypatch, caplog) -> None:
    class _ErrCtx:
        def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
            import moomoo as ft  # type: ignore[import-untyped]

            return ft.RET_ERROR, "no option volatility data entitlement"

    conn = MoomooEarningsConnector()
    conn._ctx = _ErrCtx()
    import logging

    with caplog.at_level(logging.WARNING):
        assert conn.get_underlying_iv("X") is None
    # No longer silent: the real ret message is surfaced.
    assert any("no option volatility data" in r.message for r in caplog.records)


# ── ATM-chain IV fallback ───────────────────────────────────────────────────────
class _FakeCtxATM:
    """option_volatility fails, but the option chain + snapshot work."""

    def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
        import moomoo as ft  # type: ignore[import-untyped]

        return ft.RET_ERROR, "no IV series"

    def get_market_snapshot(self, codes):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        rows = []
        for c in codes:
            sym = c.split(".")[-1]
            if any(ch.isdigit() for ch in sym):          # an option code
                rows.append({"option_implied_volatility": 48.0 if "C" in sym else 52.0,
                             "option_premium": 5.0})
            else:                                         # the underlying stock
                rows.append({"last_price": 150.0})
        return ft.RET_OK, pd.DataFrame(rows)

    def get_option_expiration_date(self, code, index_option_type="NORMAL"):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        return ft.RET_OK, pd.DataFrame({"strike_time": ["2026-07-17"]})

    def get_option_chain(self, code, start=None, end=None, **kw):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        return ft.RET_OK, pd.DataFrame({
            "code": ["US.AAPL260717C150", "US.AAPL260717P150", "US.AAPL260717C200"],
            "option_type": ["CALL", "PUT", "CALL"],
            "strike_price": [150.0, 150.0, 200.0],
        })


def test_atm_iv_fallback(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtxATM()
    iv = conn.get_atm_iv("AAPL", spot=150.0, earnings_date=date(2026, 7, 15))
    assert iv is not None
    assert iv["iv_current"] == 50.0           # avg(48, 52)
    assert iv["iv_rank"] is None              # single point — not rankable
    assert iv["expected_move"] == pytest.approx(10 / 150 * 100, abs=0.1)  # straddle 5+5
    assert iv["iv_source"] == "atm_chain"


def test_enrich_uses_atm_when_volatility_unavailable(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtxATM()
    event = EarningsEvent(ticker="AAPL", earnings_date=date(2026, 7, 15))
    assert conn.enrich_event_iv(event) is True
    assert event.iv_current == 50.0
    assert event.expected_move > 0
    assert event.source == "moomoo_iv"
    assert event.iv_rank == 0                 # ATM path can't rank → left at default


def test_enrich_event_iv_fills_fields(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtx(ivs=[20, 30, 40, 50, 45], spot=135.0)
    event = EarningsEvent(ticker="NVDA", earnings_date=date.today() + timedelta(days=10))
    assert conn.enrich_event_iv(event) is True
    assert event.iv_current == 45.0
    assert event.iv_rank == 83
    assert event.iv_percentile == 80
    assert event.stock_price == 135.0
    assert event.expected_move > 0          # derived from IV + DTE
    assert event.source == "moomoo_iv"


def test_enrich_event_iv_skips_when_opend_down(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: False)
    event = EarningsEvent(ticker="NVDA", earnings_date=date.today() + timedelta(days=10))
    assert conn.enrich_event_iv(event) is False
    assert event.iv_current == 0.0


def test_expected_move_scales_with_iv() -> None:
    far = date.today() + timedelta(days=10)
    low = MoomooEarningsConnector._expected_move(20.0, far)
    high = MoomooEarningsConnector._expected_move(60.0, far)
    assert high > low > 0


def test_provider_enriches_fmp_dates_with_moomoo_iv(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtx(ivs=[20, 30, 40, 50, 45], spot=135.0)
    manual = ManualEarningsInput("/no/file.csv")
    monkeypatch.setattr(manual, "load", lambda: [])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    # FMP supplies dates-only events; Moomoo supplies the IV.
    fmp_event = EarningsEvent(ticker="NVDA", earnings_date=date.today() + timedelta(days=8))
    monkeypatch.setattr(provider, "_fmp_calendar", lambda days: [fmp_event])
    out = provider.get_upcoming_earnings(14)
    assert len(out) == 1
    assert out[0].source == "moomoo_iv"
    assert out[0].iv_current == 45.0
    assert out[0].iv_rank == 83


def test_provider_dates_only_when_opend_down(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: False)
    manual = ManualEarningsInput("/no/file.csv")
    monkeypatch.setattr(manual, "load", lambda: [])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    fmp_event = EarningsEvent(
        ticker="NVDA", earnings_date=date.today() + timedelta(days=8), source="fmp",
    )
    monkeypatch.setattr(provider, "_fmp_calendar", lambda days: [fmp_event])
    out = provider.get_upcoming_earnings(14)
    assert len(out) == 1
    assert out[0].source == "fmp"          # not enriched
    assert out[0].iv_current == 0.0


def test_provider_csv_not_re_queried(monkeypatch) -> None:
    # CSV already has IV → provider must NOT call Moomoo enrichment.
    conn = MoomooEarningsConnector()
    called = {"enrich": 0}
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    monkeypatch.setattr(
        conn, "enrich_event_iv",
        lambda e: called.__setitem__("enrich", called["enrich"] + 1) or True,
    )
    manual = ManualEarningsInput("/no/file.csv")
    csv_event = EarningsEvent(
        ticker="CSV", earnings_date=date.today() + timedelta(days=3), iv_current=55.0,
    )
    monkeypatch.setattr(manual, "load", lambda: [csv_event])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    out = provider.get_upcoming_earnings(14)
    assert out[0].iv_current == 55.0
    assert called["enrich"] == 0           # CSV path never re-queries Moomoo
