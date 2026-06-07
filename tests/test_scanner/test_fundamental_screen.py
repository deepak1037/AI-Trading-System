"""Tests for scanner/fundamental_screen.py (Days 13-18).

The screen has two paths: FMP (when FMP_API_KEY is set) and a yfinance ``.info``
fallback. Both are exercised here with mocks — no live network calls.
"""

from __future__ import annotations

from scanner.fundamental_screen import FundamentalScreen


# ── _num helper ───────────────────────────────────────────────────────────────
class TestNum:
    def test_valid_numbers(self):
        assert FundamentalScreen._num(1.5) == 1.5
        assert FundamentalScreen._num("2.3") == 2.3
        assert FundamentalScreen._num(0) == 0.0

    def test_invalid_returns_none(self):
        assert FundamentalScreen._num(None) is None
        assert FundamentalScreen._num("n/a") is None
        assert FundamentalScreen._num([]) is None


# ── FMP path ──────────────────────────────────────────────────────────────────
def _income(eps_rev: list[tuple[float, float]]) -> list[dict]:
    """Build FMP income-statement rows (most recent first)."""
    return [{"epsdiluted": e, "revenue": r} for e, r in eps_rev]


class TestFmpPath:
    def setup_method(self):
        self.s = FundamentalScreen()
        self.s._fmp_key = "test-key"  # force FMP path

    def test_eps_qoq_growth_passes(self, monkeypatch):
        # latest EPS > prior quarter → pass
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: _income(
            [(2.0, 100), (1.5, 95)]
        ))
        assert self.s._passes_fmp("X") is True

    def test_revenue_yoy_growth_passes(self, monkeypatch):
        # flat EPS but revenue up >5% YoY (q0 vs q4) → pass
        rows = _income([(1.0, 130), (1.0, 125), (1.0, 120), (1.0, 118), (1.0, 110)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        assert self.s._passes_fmp("X") is True

    def test_no_growth_fails(self, monkeypatch):
        # declining EPS, flat revenue, no estimates → fail
        rows = _income([(1.0, 100), (1.5, 100), (2.0, 100), (2.5, 100), (3.0, 100)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: False)
        assert self.s._passes_fmp("X") is False

    def test_insufficient_data_fails(self, monkeypatch):
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: [])
        assert self.s._passes_fmp("X") is False

    def test_estimate_revision_rescues(self, monkeypatch):
        # no eps/rev growth, but rising estimates → pass
        rows = _income([(1.0, 100), (2.0, 100)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: True)
        assert self.s._passes_fmp("X") is True


# ── Fallback path (yfinance .info) ────────────────────────────────────────────
class TestFallbackPath:
    def setup_method(self):
        self.s = FundamentalScreen()
        self.s._fmp_key = ""  # force fallback

    def _patch_info(self, monkeypatch, info: dict):
        import sys
        import types
        fake_yf = types.ModuleType("yfinance")

        class _T:
            def __init__(self, *a, **k):
                self.info = info

        fake_yf.Ticker = _T  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    def test_profitable_and_growing_passes(self, monkeypatch):
        self._patch_info(monkeypatch, {"trailingEps": 3.2, "revenueGrowth": 0.08})
        assert self.s._passes_fallback("X") is True

    def test_strong_earnings_growth_passes(self, monkeypatch):
        self._patch_info(monkeypatch, {"trailingEps": -0.5, "earningsGrowth": 0.4})
        assert self.s._passes_fallback("X") is True

    def test_high_revenue_growth_passes(self, monkeypatch):
        self._patch_info(monkeypatch, {"revenueGrowth": 0.15})
        assert self.s._passes_fallback("X") is True

    def test_declining_unprofitable_fails(self, monkeypatch):
        self._patch_info(monkeypatch, {
            "trailingEps": -1.0, "revenueGrowth": -0.05,
            "earningsGrowth": -0.2, "grossMargins": 0.1,
        })
        assert self.s._passes_fallback("X") is False

    def test_empty_info_fails(self, monkeypatch):
        self._patch_info(monkeypatch, {})
        assert self.s._passes_fallback("X") is False


# ── Integration ───────────────────────────────────────────────────────────────
class TestFundamentalScreenIntegration:
    def test_screen_empty_tickers(self):
        assert FundamentalScreen().screen([]) == []

    def test_screen_routes_to_fallback_when_no_key(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = ""
        calls = {"fallback": 0, "fmp": 0}
        monkeypatch.setattr(s, "_passes_fallback", lambda t: calls.__setitem__("fallback", calls["fallback"] + 1) or True)
        monkeypatch.setattr(s, "_passes_fmp", lambda t: calls.__setitem__("fmp", calls["fmp"] + 1) or True)
        result = s.screen(["AAPL", "MSFT"])
        assert result == ["AAPL", "MSFT"]
        assert calls["fallback"] == 2 and calls["fmp"] == 0

    def test_screen_routes_to_fmp_when_key_set(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = "test-key"
        calls = {"fallback": 0, "fmp": 0}
        monkeypatch.setattr(s, "_passes_fallback", lambda t: calls.__setitem__("fallback", calls["fallback"] + 1) or True)
        monkeypatch.setattr(s, "_passes_fmp", lambda t: calls.__setitem__("fmp", calls["fmp"] + 1) or True)
        s.screen(["AAPL"])
        assert calls["fmp"] == 1 and calls["fallback"] == 0

    def test_screen_survives_per_ticker_errors(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = ""

        def _boom(t):
            if t == "BAD":
                raise RuntimeError("data error")
            return True

        monkeypatch.setattr(s, "_passes_fallback", _boom)
        result = s.screen(["GOOD", "BAD", "GOOD2"])
        assert result == ["GOOD", "GOOD2"]  # BAD skipped, no crash
