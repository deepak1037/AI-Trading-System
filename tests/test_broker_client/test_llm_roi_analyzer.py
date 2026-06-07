"""Tests for broker_client/llm_roi_analyzer.py.

Covers: ROI math (reg_t + cash_secured margin), DTE, contract lookup, JSON
parsing (clean / fenced / prose-wrapped / invalid), the no-API-key guard, and
the full analyze path with a mocked Anthropic client + stubbed context.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from broker_client.llm_roi_analyzer import (
    LLMROIAnalysis,
    LLMROIAnalyzer,
    PutROIResult,
)
from broker_core.base_broker import (
    Account,
    OptionsChain,
    OptionsContract,
    OrderPreview,
    Quote,
)
from core.exceptions import DataError

COMMISSION = 0.65  # settings.PAPER_OPTIONS_COMMISSION default


# ── Fakes ─────────────────────────────────────────────────────────────────────
def _future_expiry(days: int = 40) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class FakeBroker:
    """Duck-typed broker covering the methods the analyzer touches."""

    def __init__(
        self,
        chain: OptionsChain,
        last: float = 12.90,
        buying_power: float = 10_000.0,
        projected_bp: float = 9_000.0,
        preview_valid: bool = True,
    ) -> None:
        self._chain = chain
        self._last = last
        self._buying_power = buying_power
        self._projected_bp = projected_bp
        self._preview_valid = preview_valid

    def get_options_chain(self, ticker: str, expiry=None) -> OptionsChain:
        return self._chain

    def get_quote(self, ticker: str) -> Quote:
        return Quote(ticker=ticker, bid=self._last - 0.01, ask=self._last + 0.01, last=self._last)

    def get_account(self) -> Account:
        return Account(account_id="X", buying_power=self._buying_power)

    def preview_options_order(self, order) -> OrderPreview:
        return OrderPreview(
            estimated_cost=0.0,
            buying_power_effect=self._projected_bp,
            is_valid=self._preview_valid,
            rejection_reason=None if self._preview_valid else "rejected",
        )


def _make_chain(expiry: str, ticker: str = "HOOD") -> OptionsChain:
    # strike-8 put: bid 0.78 / ask 0.82 → mid 0.80; delta on the 8 strike,
    # a ±999 sentinel on the 7 strike to exercise the Greeks guard.
    puts = [
        OptionsContract(symbol=f"{ticker}P7", expiry=expiry, strike=7.0,
                        option_type="put", bid=0.40, ask=0.46, delta=-999.0),
        OptionsContract(symbol=f"{ticker}P8", expiry=expiry, strike=8.0,
                        option_type="put", bid=0.78, ask=0.82,
                        delta=-0.25, implied_vol=0.62),
        OptionsContract(symbol=f"{ticker}P9", expiry=expiry, strike=9.0,
                        option_type="put", bid=1.20, ask=1.28),
    ]
    return OptionsChain(ticker=ticker, expiry=expiry, calls=[], puts=puts)


def _make_multi_expiry_chain(expiries: list[str], ticker: str = "HOOD") -> OptionsChain:
    """A single chain spanning several expiries (as Schwab's nearest call returns)."""
    puts = []
    for i, exp in enumerate(expiries):
        for strike in (7.0, 8.0, 9.0):
            puts.append(OptionsContract(
                symbol=f"{ticker}{exp}P{int(strike)}", expiry=exp, strike=strike,
                option_type="put", bid=0.70 + i * 0.1, ask=0.80 + i * 0.1,
                delta=-0.25,
            ))
    return OptionsChain(ticker=ticker, expiry="nearest", calls=[], puts=puts)


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._payload)


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self.messages = _FakeMessages(payload)


_VALID_ASSESSMENT = {
    "expiry_worthless_probability": 78,
    "expected_50pct_decay_days": 8,
    "adjusted_monthly_roi_pct": 31.5,
    "key_risks": ["earnings risk", "sector rotation"],
    "bounce_scenario": "Oversold, historical bounce within 5-10 days",
    "assignment_scenario": "Drop below $8 if selloff continues",
    "recommendation": "STRONG_SELL",
    "confidence": 82,
    "reasoning": "High IV premium with oversold technicals favors the seller.",
}


@pytest.fixture
def analyzer():
    expiry = _future_expiry(40)
    broker = FakeBroker(_make_chain(expiry))
    a = LLMROIAnalyzer(broker=broker)
    return a, expiry


# ── Margin math ───────────────────────────────────────────────────────────────
class TestMargin:
    def test_reg_t_margin(self, monkeypatch):
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.OPTIONS_MARGIN_BASIS", "reg_t")
        a = LLMROIAnalyzer()
        # underlying 12.9, strike 8, premium 0.80
        # otm = 12.9-8 = 4.9; req = max(0.2*12.9-4.9, 0.1*8)=max(-2.32,0.8)=0.8
        # margin/share = 0.8+0.8 = 1.6 → 160/contract
        m = a._margin_per_contract(8.0, 12.9, 0.80)
        assert m == pytest.approx(160.0)

    def test_reg_t_margin_atm(self, monkeypatch):
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.OPTIONS_MARGIN_BASIS", "reg_t")
        a = LLMROIAnalyzer()
        # underlying 100, strike 100, premium 3 → otm=0; req=max(20,10)=20; +3=23 → 2300
        m = a._margin_per_contract(100.0, 100.0, 3.0)
        assert m == pytest.approx(2300.0)

    def test_cash_secured_margin(self, monkeypatch):
        monkeypatch.setattr(
            "broker_client.llm_roi_analyzer.settings.OPTIONS_MARGIN_BASIS", "cash_secured"
        )
        a = LLMROIAnalyzer()
        # (strike - premium) * 100 = (8 - 0.8) * 100 = 720
        m = a._margin_per_contract(8.0, 12.9, 0.80)
        assert m == pytest.approx(720.0)


class TestCleanGreek:
    def test_passes_normal_values(self):
        assert LLMROIAnalyzer._clean_greek(-0.25) == pytest.approx(-0.25)
        assert LLMROIAnalyzer._clean_greek(0.0) == 0.0

    def test_rejects_sentinels_and_none(self):
        assert LLMROIAnalyzer._clean_greek(-999.0) is None
        assert LLMROIAnalyzer._clean_greek(999.0) is None
        assert LLMROIAnalyzer._clean_greek(900.0) is None
        assert LLMROIAnalyzer._clean_greek(None) is None
        assert LLMROIAnalyzer._clean_greek("bad") is None


class TestDaysToExpiry:
    def test_future(self):
        a = LLMROIAnalyzer()
        assert a._days_to_expiry(_future_expiry(10)) == 10

    def test_past_clamped_to_zero(self):
        a = LLMROIAnalyzer()
        past = (date.today() - timedelta(days=5)).isoformat()
        assert a._days_to_expiry(past) == 0


# ── Contract lookup ───────────────────────────────────────────────────────────
class TestFindPut:
    def test_exact_strike(self, analyzer):
        a, expiry = analyzer
        c = a._find_put("HOOD", 8.0, expiry)
        assert c.strike == 8.0

    def test_closest_strike_fallback(self, analyzer):
        a, expiry = analyzer
        c = a._find_put("HOOD", 8.4, expiry)  # no 8.4 → closest is 8.0
        assert c.strike == 8.0

    def test_no_puts_raises(self):
        empty = OptionsChain(ticker="X", expiry=_future_expiry(), calls=[], puts=[])
        a = LLMROIAnalyzer(broker=FakeBroker(empty))
        with pytest.raises(DataError):
            a._find_put("X", 8.0, _future_expiry())


# ── build_put_roi ─────────────────────────────────────────────────────────────
class TestBuildPutROI:
    def test_basic_roi_net_premium_and_dual(self, analyzer, monkeypatch):
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.OPTIONS_MARGIN_BASIS", "reg_t")
        a, expiry = analyzer
        roi = a.build_put_roi("HOOD", 8.0, expiry)  # formula margin (no broker preview)
        assert isinstance(roi, PutROIResult)
        assert roi.ticker == "HOOD"
        assert roi.mid == pytest.approx(0.80, abs=0.001)
        # Net premium = gross 80 − commission 0.65 = 79.35
        assert roi.gross_premium_per_contract == pytest.approx(80.0, abs=0.01)
        assert roi.commission_per_contract == pytest.approx(COMMISSION, abs=0.01)
        assert roi.premium_per_contract == pytest.approx(79.35, abs=0.01)
        assert roi.margin_basis == "reg_t"
        assert roi.margin_per_contract == pytest.approx(160.0, abs=0.5)
        # breakeven = strike − net premium/share = 8 − 0.7935
        assert roi.breakeven == pytest.approx(7.21, abs=0.01)
        assert roi.otm_pct == pytest.approx(37.98, abs=0.1)
        # static (margin) = 79.35/160; monthly scales by 30/DTE
        assert roi.static_roi_pct == pytest.approx(79.35 / 160 * 100, abs=0.2)
        assert roi.monthly_roi_pct == pytest.approx(
            roi.static_roi_pct * 30.0 / roi.days_to_expiry, abs=0.2
        )
        # Cash-secured companion: capital = strike × 100 = 800
        assert roi.cash_secured_margin_per_contract == pytest.approx(800.0)
        assert roi.cash_secured_roi_pct == pytest.approx(79.35 / 800 * 100, abs=0.2)

    def test_delta_and_iv_from_contract(self, analyzer):
        a, expiry = analyzer
        roi = a.build_put_roi("HOOD", 8.0, expiry)
        assert roi.delta == pytest.approx(-0.25, abs=0.001)
        assert roi.iv == pytest.approx(62.0, abs=0.1)  # 0.62 fraction → 62%

    def test_iv_normalizer(self):
        # yfinance fraction → percent; Schwab percent kept as-is; sentinels dropped.
        assert LLMROIAnalyzer._normalize_iv(0.62) == pytest.approx(62.0)
        assert LLMROIAnalyzer._normalize_iv(70.82) == pytest.approx(70.8, abs=0.1)
        assert LLMROIAnalyzer._normalize_iv(-999.0) is None
        assert LLMROIAnalyzer._normalize_iv(0.0) is None
        assert LLMROIAnalyzer._normalize_iv(None) is None

    def test_greek_sentinel_rejected(self, analyzer):
        a, expiry = analyzer
        roi = a.build_put_roi("HOOD", 7.0, expiry)  # P7 has delta=-999 sentinel
        assert roi.delta is None

    def test_real_margin_via_preview(self, analyzer, monkeypatch):
        a, expiry = analyzer
        # FakeBroker: BP 10000 → projected 9000 → reduction 1000 = margin.
        roi = a.build_put_roi("HOOD", 8.0, expiry, use_broker_margin=True)
        assert roi.margin_basis == "schwab_preview"
        assert roi.margin_per_contract == pytest.approx(1000.0)

    def test_benign_alert_still_uses_real_margin(self, monkeypatch):
        # Schwab flags naked-put previews with a benign alert (is_valid=False)
        # but the projected balance is still usable — we must NOT discard it.
        expiry = _future_expiry(40)
        broker = FakeBroker(_make_chain(expiry), preview_valid=False,
                            buying_power=10_000.0, projected_bp=9_000.0)
        a = LLMROIAnalyzer(broker=broker)
        roi = a.build_put_roi("HOOD", 8.0, expiry, use_broker_margin=True)
        assert roi.margin_basis == "schwab_preview"
        assert roi.margin_per_contract == pytest.approx(1000.0)

    def test_nonpositive_reduction_falls_back_to_formula(self, monkeypatch):
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.OPTIONS_MARGIN_BASIS", "reg_t")
        expiry = _future_expiry(40)
        # projected == buying_power → reduction 0 → formula fallback.
        broker = FakeBroker(_make_chain(expiry), buying_power=10_000.0, projected_bp=10_000.0)
        a = LLMROIAnalyzer(broker=broker)
        roi = a.build_put_roi("HOOD", 8.0, expiry, use_broker_margin=True)
        assert roi.margin_basis == "reg_t"
        assert roi.margin_per_contract == pytest.approx(160.0, abs=0.5)

    def test_explicit_price_overrides_broker(self, analyzer):
        a, expiry = analyzer
        roi = a.build_put_roi("HOOD", 8.0, expiry, underlying_price=10.0)
        assert roi.underlying_price == 10.0

    def test_zero_price_raises(self):
        expiry = _future_expiry()
        chain = OptionsChain(
            ticker="Z", expiry=expiry, calls=[],
            puts=[OptionsContract(symbol="Z", expiry=expiry, strike=8.0,
                                  option_type="put", bid=0.0, ask=0.0, last=0.0)],
        )
        a = LLMROIAnalyzer(broker=FakeBroker(chain))
        with pytest.raises(DataError):
            a.build_put_roi("Z", 8.0, expiry, underlying_price=12.0)


# ── Expiry listing + ROI table (single chain fetch) ───────────────────────────
class TestListExpiries:
    def test_broker_first(self):
        exps = [_future_expiry(10), _future_expiry(40), _future_expiry(70)]
        broker = FakeBroker(_make_multi_expiry_chain(exps))
        a = LLMROIAnalyzer(broker=broker)
        out = a.list_expiries("HOOD")
        assert out == sorted(exps)  # soonest-first, deduped

    def test_limit_applied(self):
        exps = [_future_expiry(d) for d in (5, 12, 19, 26, 33)]
        broker = FakeBroker(_make_multi_expiry_chain(exps))
        a = LLMROIAnalyzer(broker=broker)
        assert len(a.list_expiries("HOOD", limit=3)) == 3

    def test_no_broker_returns_empty_or_yf(self, monkeypatch):
        # No broker → tries yfinance; force it to fail → empty list (no raise).
        a = LLMROIAnalyzer(broker=None)
        import sys
        import types
        fake_yf = types.ModuleType("yfinance")
        def _boom(*args, **kwargs):
            raise RuntimeError("rate limited")
        fake_yf.Ticker = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
        assert a.list_expiries("HOOD") == []


class TestBuildRoiTable:
    def test_one_fetch_all_expiries(self):
        exps = [_future_expiry(10), _future_expiry(40), _future_expiry(70)]
        broker = FakeBroker(_make_multi_expiry_chain(exps), last=12.9)
        a = LLMROIAnalyzer(broker=broker)
        table = a.build_roi_table("HOOD", 8.0, underlying_price=12.9)
        assert len(table) == 3
        assert all(isinstance(r, PutROIResult) for r in table)
        assert [r.expiry for r in table] == sorted(exps)
        # All rows are for the requested strike, formula margin (no preview).
        assert all(r.strike == 8.0 for r in table)
        assert all(r.margin_basis in ("reg_t", "cash_secured") for r in table)

    def test_closest_strike_when_exact_missing(self):
        exps = [_future_expiry(10)]
        broker = FakeBroker(_make_multi_expiry_chain(exps))
        a = LLMROIAnalyzer(broker=broker)
        table = a.build_roi_table("HOOD", 8.4, underlying_price=12.9)  # no 8.4 → 8.0
        assert len(table) == 1
        assert table[0].strike == 8.0

    def test_no_broker_returns_empty(self):
        a = LLMROIAnalyzer(broker=None)
        assert a.build_roi_table("HOOD", 8.0) == []


# ── JSON parsing ──────────────────────────────────────────────────────────────
class TestParseAssessment:
    def test_clean_json(self):
        out = LLMROIAnalyzer._parse_assessment(json.dumps(_VALID_ASSESSMENT))
        assert out.recommendation == "STRONG_SELL"
        assert out.adjusted_monthly_roi_pct == 31.5

    def test_fenced_json(self):
        fenced = "```json\n" + json.dumps(_VALID_ASSESSMENT) + "\n```"
        out = LLMROIAnalyzer._parse_assessment(fenced)
        assert out.confidence == 82

    def test_prose_wrapped_json(self):
        prose = "Here is my analysis:\n" + json.dumps(_VALID_ASSESSMENT) + "\nHope that helps."
        out = LLMROIAnalyzer._parse_assessment(prose)
        assert out.expiry_worthless_probability == 78

    def test_no_json_raises(self):
        with pytest.raises(DataError):
            LLMROIAnalyzer._parse_assessment("no json here at all")

    def test_malformed_json_raises(self):
        with pytest.raises(DataError):
            LLMROIAnalyzer._parse_assessment("{ not valid json, }")

    def test_out_of_range_probability_raises(self):
        bad = dict(_VALID_ASSESSMENT, expiry_worthless_probability=150)
        with pytest.raises(Exception):  # pydantic ValidationError
            LLMROIAnalyzer._parse_assessment(json.dumps(bad))


# ── LLM client guard ──────────────────────────────────────────────────────────
class TestClientGuard:
    def test_no_api_key_raises(self, monkeypatch):
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.ANTHROPIC_API_KEY", "")
        a = LLMROIAnalyzer()
        with pytest.raises(DataError, match="ANTHROPIC_API_KEY"):
            a._ensure_client()


# ── Full analyze path (mocked LLM + stubbed context) ──────────────────────────
class TestAnalyzePutOpportunity:
    def test_combines_roi_and_assessment(self, analyzer, monkeypatch):
        a, expiry = analyzer
        monkeypatch.setattr(
            "broker_client.llm_roi_analyzer.settings.ANTHROPIC_API_KEY", "sk-test"
        )
        # Stub context so no network is hit.
        monkeypatch.setattr(a, "_gather_context", lambda t, r: {"current_price": 12.9})
        # Inject a fake Anthropic client.
        a._client = _FakeClient(json.dumps(_VALID_ASSESSMENT))

        roi = a.build_put_roi("HOOD", 8.0, expiry)
        analysis = a.analyze_put_opportunity(roi, "HOOD")

        assert isinstance(analysis, LLMROIAnalysis)
        assert analysis.roi.ticker == "HOOD"
        assert analysis.assessment.recommendation == "STRONG_SELL"
        assert analysis.context == {"current_price": 12.9}
        # The fake client was actually called with the configured model.
        assert a._client.messages.calls[0]["model"] == "claude-sonnet-4-6"

    def test_retries_then_fails(self, analyzer, monkeypatch):
        a, expiry = analyzer
        monkeypatch.setattr(
            "broker_client.llm_roi_analyzer.settings.ANTHROPIC_API_KEY", "sk-test"
        )
        monkeypatch.setattr("broker_client.llm_roi_analyzer.settings.API_MAX_RETRIES", 2)
        monkeypatch.setattr(a, "_gather_context", lambda t, r: {})

        class _BoomMessages:
            def __init__(self):
                self.n = 0

            def create(self, **kwargs):
                self.n += 1
                raise RuntimeError("api down")

        class _BoomClient:
            def __init__(self):
                self.messages = _BoomMessages()

        a._client = _BoomClient()
        roi = a.build_put_roi("HOOD", 8.0, expiry)
        with pytest.raises(DataError, match="failed after 2 attempts"):
            a.analyze_put_opportunity(roi, "HOOD")
        assert a._client.messages.n == 2
