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
    """Returns an option-volatility IV series (for _iv_rank_from_option tests)."""

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


class _FakeCtxFull:
    """Full ATM path: stock snapshot, expiry, chain, ATM option snapshot, and an
    option IV series for the rank computation."""

    def __init__(self, spot=291.13, call_iv=48.0, put_iv=52.0, rank_ivs=None) -> None:
        self._spot = spot
        self._call_iv = call_iv
        self._put_iv = put_iv
        self._rank_ivs = rank_ivs if rank_ivs is not None else [30, 40, 60, 50, 55]

    def get_market_snapshot(self, codes):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        rows = []
        for c in codes:
            sym = c.split(".")[-1]
            if any(ch.isdigit() for ch in sym):  # option code
                rows.append({
                    "option_implied_volatility": self._call_iv if "C" in sym else self._put_iv,
                    "option_premium": 2.0,    # extrinsic only — must NOT be used
                    "last_price": 6.0,        # the real option price → straddle 12
                })
            else:
                rows.append({"last_price": self._spot})
        return ft.RET_OK, pd.DataFrame(rows)

    def get_option_expiration_date(self, code, index_option_type="NORMAL"):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        # An EXPIRED date first, then a live one — exercises the skip-expired logic.
        return ft.RET_OK, pd.DataFrame({"strike_time": ["2020-01-17", "2026-06-15"]})

    def get_option_chain(self, code, start=None, end=None, **kw):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        return ft.RET_OK, pd.DataFrame({
            "code": ["US.AAPL260615C291000", "US.AAPL260615P291000",
                     "US.AAPL260615C300000"],
            "option_type": ["CALL", "PUT", "CALL"],
            "strike_price": [291.0, 291.0, 300.0],
        })

    def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        df = pd.DataFrame({
            "timestamp": list(range(len(self._rank_ivs))),
            "implied_volatility": self._rank_ivs,
            "history_volatility": [25.0] * len(self._rank_ivs),
        })
        return ft.RET_OK, df

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
    monkeypatch.setattr(conn, "_atm_call_code", lambda t: "US.NVDA260618C170000")
    ed = date.today() - timedelta(days=90)
    monkeypatch.setattr(
        conn, "_iv_series",
        lambda code: [(ed - timedelta(days=1), 60.0), (ed + timedelta(days=2), 35.0)],
    )
    monkeypatch.setattr(mod, "_past_earnings_dates", lambda t, **kw: [ed])
    monkeypatch.setattr(mod, "_earnings_moves", lambda t, dates: {ed: -8.0})

    hist = conn.get_earnings_iv_history("NVDA")
    assert hist.count == 1
    q = hist.quarters[0]
    assert q.iv_crush == pytest.approx(41.67, abs=0.1)
    assert q.actual_move_close == -8.0


def test_get_earnings_iv_history_multi_quarter(monkeypatch) -> None:
    # Realistic: a daily IV series spanning several past earnings → per-quarter crush.
    import broker_client.earnings.moomoo_earnings as mod

    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    monkeypatch.setattr(conn, "_atm_call_code", lambda t: "US.ACN260618C170000")

    today = date.today()
    quarters_ago = [today - timedelta(days=d) for d in (270, 180, 90)]
    # Build a daily series: baseline 28%, ramping to ~50% before each print, 30% after.
    series: list[tuple[date, float]] = []
    cur = today - timedelta(days=300)
    while cur <= today:
        iv = 28.0
        for ed in quarters_ago:
            days_to = (ed - cur).days
            if 0 <= days_to <= 5:      # ramping up before earnings
                iv = max(iv, 50.0 - days_to * 2)
            elif -5 <= days_to < 0:    # collapsed after
                iv = 30.0
        series.append((cur, iv))
        cur += timedelta(days=1)

    monkeypatch.setattr(conn, "_iv_series", lambda code: series)
    monkeypatch.setattr(mod, "_past_earnings_dates", lambda t, **kw: quarters_ago)
    monkeypatch.setattr(mod, "_earnings_moves", lambda t, dates: {d: -4.0 for d in dates})

    hist = conn.get_earnings_iv_history("ACN")
    assert hist.count == 3                       # one per past earnings in the series
    crushes = [q.iv_crush for q in hist.quarters]
    assert all(c > 15 for c in crushes)          # ~ (50-30)/50 = 40%
    assert all(q.actual_move_close == -4.0 for q in hist.quarters)


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


def test_iv_rank_from_option_computes_rank(monkeypatch) -> None:
    # _iv_rank_from_option ranks an ATM CONTRACT's IV series (option code).
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtx(ivs=[20, 30, 40, 50, 45])  # latest = 45
    rank = conn._iv_rank_from_option("US.AAPL260615C291000")
    assert rank["iv_rank"] == 83            # (45-20)/(50-20)*100
    assert rank["iv_percentile"] == 80      # 4 of 5 values <= 45


def test_iv_rank_from_option_empty_returns_none() -> None:
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtx(ivs=[])
    rank = conn._iv_rank_from_option("US.AAPL260615C291000")
    assert rank["iv_rank"] is None and rank["iv_percentile"] is None


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
                {"timestamp": [1, 2], "implied_volatility": [30.0, 45.0]}
            )
            return ft.RET_OK, df

    conn = MoomooEarningsConnector()
    rec = _RecCtx()
    conn._ctx = rec
    conn._iv_rank_from_option("US.AAPL260615C291000")
    assert rec.period == 5                    # the corrected enum value


def test_option_volatility_df_logs_ret_error(caplog) -> None:
    class _ErrCtx:
        def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
            import moomoo as ft  # type: ignore[import-untyped]

            return ft.RET_ERROR, "Only option codes are supported"

    conn = MoomooEarningsConnector()
    conn._ctx = _ErrCtx()
    import logging

    with caplog.at_level(logging.DEBUG, logger="broker_client.earnings.moomoo_earnings"):
        assert conn._option_volatility_df("US.AAPL") is None
    # No longer silent: the real ret message is surfaced (at DEBUG — expected case).
    assert any("Only option codes are supported" in r.message for r in caplog.records)


# ── ATM-chain IV (the live path) ────────────────────────────────────────────────
class _FakeCtxATM:
    """option_volatility fails (no rank), but the ATM chain + snapshot work."""

    def get_option_volatility(self, code, query_time_period=None, hv_time_period=None):
        import moomoo as ft  # type: ignore[import-untyped]

        return ft.RET_ERROR, "Only option codes are supported"

    def get_market_snapshot(self, codes):
        import moomoo as ft  # type: ignore[import-untyped]
        import pandas as pd

        rows = []
        for c in codes:
            sym = c.split(".")[-1]
            if any(ch.isdigit() for ch in sym):          # an option code
                rows.append({"option_implied_volatility": 48.0 if "C" in sym else 52.0,
                             "option_premium": 2.0, "last_price": 5.0})  # price → straddle 10
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


def test_nearest_expiry_skips_expired() -> None:
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtxFull()  # expiries: 2020-01-17 (expired), 2026-06-15 (live)
    expiry = MoomooEarningsConnector._nearest_expiry(conn._ctx, "US.AAPL", date(2026, 6, 14))
    assert expiry == "2026-06-15"             # the expired one is skipped


def test_nearest_expiry_none_when_all_expired() -> None:
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtxFull()
    assert MoomooEarningsConnector._nearest_expiry(conn._ctx, "US.AAPL", date(2030, 1, 1)) is None


def test_atm_iv_full_path_with_rank() -> None:
    # Full ATM path: snapshot IV + straddle expected move + rank from contract series.
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtxFull(spot=291.13, call_iv=48.0, put_iv=52.0,
                             rank_ivs=[30, 40, 60, 50, 55])  # latest 55
    iv = conn.get_atm_iv("AAPL", spot=291.13, earnings_date=date(2026, 6, 15))
    assert iv["iv_current"] == 50.0           # avg(48, 52)
    assert iv["expected_move"] == pytest.approx(12 / 291.13 * 100, abs=0.1)  # straddle 6+6
    assert iv["iv_rank"] == 83                 # (55-30)/(60-30)*100 from the contract series
    assert iv["iv_source"] == "atm_chain"


def test_option_price_prefers_mid_then_last() -> None:
    from broker_client.earnings.moomoo_earnings import _option_price

    assert _option_price({"bid_price": 7.0, "ask_price": 8.0, "last_price": 7.5}) == 7.5  # mid
    assert _option_price({"bid_price": 0, "ask_price": 0, "last_price": 7.5}) == 7.5       # last
    assert _option_price({"option_premium": 4.24}) is None                                 # never premium


def test_expected_move_uses_price_not_premium() -> None:
    # ACN-style: premium (extrinsic) 4.24/4.29 but real price ~7.5/7.0.
    import pandas as pd

    snap = pd.DataFrame([
        {"option_implied_volatility": 50.0, "option_premium": 4.24,
         "bid_price": 7.3, "ask_price": 8.0, "last_price": 7.5},   # call mid 7.65
        {"option_implied_volatility": 50.0, "option_premium": 4.29,
         "bid_price": 6.6, "ask_price": 7.7, "last_price": 7.03},  # put mid 7.15
    ])
    atm = MoomooEarningsConnector._atm_from_snapshot(snap, spot=170.28)
    # straddle = 7.65 + 7.15 = 14.80 → 8.69% (NOT premium 8.53/170 = 5.0%)
    assert atm["expected_move"] == pytest.approx(14.80 / 170.28 * 100, abs=0.1)
    assert atm["expected_move"] > 8.0          # would be ~5% if premium were (wrongly) used


def test_atm_iv_without_rank(monkeypatch) -> None:
    # option_volatility errors → ATM IV still works, rank absent.
    conn = MoomooEarningsConnector()
    conn._ctx = _FakeCtxATM()
    iv = conn.get_atm_iv("AAPL", spot=150.0, earnings_date=date(2026, 7, 15))
    assert iv["iv_current"] == 50.0
    assert iv["iv_rank"] is None
    assert iv["expected_move"] == pytest.approx(10 / 150 * 100, abs=0.1)


def test_enrich_event_iv_full_path(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtxFull(spot=291.13, rank_ivs=[30, 40, 60, 50, 55])
    event = EarningsEvent(ticker="AAPL", earnings_date=date(2026, 6, 15))
    assert conn.enrich_event_iv(event) is True
    assert event.iv_current == 50.0
    assert event.iv_rank == 83
    assert event.expected_move > 0
    assert event.stock_price == 291.13
    assert event.source == "moomoo_iv"


def test_expected_move_scales_with_iv() -> None:
    far = date.today() + timedelta(days=10)
    low = MoomooEarningsConnector._expected_move(20.0, far)
    high = MoomooEarningsConnector._expected_move(60.0, far)
    assert high > low > 0


def test_provider_enriches_fmp_dates_with_moomoo_iv(monkeypatch) -> None:
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    conn._ctx = _FakeCtxFull(spot=291.13, rank_ivs=[30, 40, 60, 50, 55])
    manual = ManualEarningsInput("/no/file.csv")
    monkeypatch.setattr(manual, "load", lambda: [])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    # FMP supplies dates-only events; Moomoo supplies the IV.
    fmp_event = EarningsEvent(ticker="AAPL", earnings_date=date(2026, 6, 15))
    monkeypatch.setattr(provider, "_fmp_calendar", lambda days: [fmp_event])
    out = provider.get_upcoming_earnings(60)
    assert len(out) == 1
    assert out[0].source == "moomoo_iv"
    assert out[0].iv_current == 50.0
    assert out[0].iv_rank == 83


def test_get_ticker_earnings_resolves_date_and_enriches(monkeypatch) -> None:
    import broker_client.earnings.moomoo_earnings as mod

    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    monkeypatch.setattr(
        conn, "enrich_event_iv",
        lambda e: (setattr(e, "iv_current", 60.0) or setattr(e, "iv_rank", 80)
                   or setattr(e, "source", "moomoo_iv") or True),
    )
    provider = EarningsDataProvider(connector=conn)
    # FMP per-symbol has no date; yfinance supplies it.
    monkeypatch.setattr(mod, "_next_earnings_fmp", lambda t: None)
    monkeypatch.setattr(mod, "_yfinance_next_date", lambda t: date.today() + timedelta(days=6))

    event = provider.get_ticker_earnings("ACN", days_ahead=14)
    assert event is not None
    assert event.ticker == "ACN"
    assert event.source == "moomoo_iv"
    assert event.iv_rank == 80


def test_get_ticker_earnings_none_when_no_date(monkeypatch) -> None:
    import broker_client.earnings.moomoo_earnings as mod

    provider = EarningsDataProvider(connector=MoomooEarningsConnector())
    monkeypatch.setattr(mod, "_next_earnings_fmp", lambda t: None)
    monkeypatch.setattr(mod, "_yfinance_next_date", lambda t: None)
    assert provider.get_ticker_earnings("ZZZZ", days_ahead=14) is None


def test_get_ticker_earnings_out_of_window(monkeypatch) -> None:
    import broker_client.earnings.moomoo_earnings as mod

    provider = EarningsDataProvider(connector=MoomooEarningsConnector())
    monkeypatch.setattr(mod, "_next_earnings_fmp", lambda t: date.today() + timedelta(days=60))
    monkeypatch.setattr(mod, "_yfinance_next_date", lambda t: None)
    assert provider.get_ticker_earnings("ACN", days_ahead=14) is None  # 60d > 14d window


def test_provider_only_ticker_limits_enrichment(monkeypatch) -> None:
    # only_ticker → just that symbol is IV-enriched; others stay dates-only.
    conn = MoomooEarningsConnector()
    monkeypatch.setattr(conn, "_opend_reachable", lambda timeout=1.0: True)
    enriched_calls = []

    def fake_enrich(event):
        enriched_calls.append(event.ticker)
        event.iv_current = 50.0
        event.source = "moomoo_iv"
        return True

    monkeypatch.setattr(conn, "enrich_event_iv", fake_enrich)
    manual = ManualEarningsInput("/no/file.csv")
    monkeypatch.setattr(manual, "load", lambda: [])
    provider = EarningsDataProvider(connector=conn, manual=manual)
    events = [
        EarningsEvent(ticker="AAPL", earnings_date=date.today() + timedelta(days=5), source="fmp"),
        EarningsEvent(ticker="MSFT", earnings_date=date.today() + timedelta(days=6), source="fmp"),
        EarningsEvent(ticker="ACN", earnings_date=date.today() + timedelta(days=7), source="fmp"),
    ]
    monkeypatch.setattr(provider, "_fmp_calendar", lambda days: events)
    out = provider.get_upcoming_earnings(14, only_ticker="acn")
    assert enriched_calls == ["ACN"]                 # only ACN queried Moomoo
    acn = next(e for e in out if e.ticker == "ACN")
    assert acn.source == "moomoo_iv"
    assert all(e.source == "fmp" for e in out if e.ticker != "ACN")


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
