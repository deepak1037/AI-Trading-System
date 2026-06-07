"""Tests for scanner/fundamental_screen.py (Days 13-18).

Two paths: FMP (when FMP_API_KEY set) and a yfinance ``.info`` fallback. Both
now return per-stock flag dicts ({eps_accelerating, rev_reaccelerating,
est_revisions_up}) for scoring, or None when failing. Mocked — no live calls.
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
    # Stable-API field name is camelCase epsDiluted.
    return [{"epsDiluted": e, "revenue": r} for e, r in eps_rev]


class TestFmpFlags:
    def setup_method(self):
        self.s = FundamentalScreen()
        self.s._fmp_key = "test-key"

    def test_eps_qoq_growth(self, monkeypatch):
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: _income([(2.0, 100), (1.5, 95)]))
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: False)
        flags = self.s._fmp_flags("X")
        assert flags is not None and flags["eps_accelerating"] is True

    def test_revenue_yoy_growth(self, monkeypatch):
        rows = _income([(1.0, 130), (1.0, 125), (1.0, 120), (1.0, 118), (1.0, 110)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: False)
        flags = self.s._fmp_flags("X")
        assert flags is not None and flags["rev_reaccelerating"] is True

    def test_no_growth_returns_none(self, monkeypatch):
        rows = _income([(1.0, 100), (1.5, 100), (2.0, 100), (2.5, 100), (3.0, 100)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: False)
        assert self.s._fmp_flags("X") is None

    def test_insufficient_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: [])
        assert self.s._fmp_flags("X") is None

    def test_estimate_revision_rescues(self, monkeypatch):
        rows = _income([(1.0, 100), (2.0, 100)])
        monkeypatch.setattr(self.s, "_fmp_get", lambda path, **k: rows)
        monkeypatch.setattr(self.s, "_fmp_estimates_positive", lambda t: True)
        flags = self.s._fmp_flags("X")
        assert flags is not None and flags["est_revisions_up"] is True


# ── Fallback path (yfinance .info) ────────────────────────────────────────────
class TestFallbackFlags:
    def setup_method(self):
        self.s = FundamentalScreen()
        self.s._fmp_key = ""

    def _patch_info(self, monkeypatch, info: dict):
        import sys
        import types
        fake_yf = types.ModuleType("yfinance")

        class _T:
            def __init__(self, *a, **k):
                self.info = info

        fake_yf.Ticker = _T  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    def test_growing_revenue(self, monkeypatch):
        self._patch_info(monkeypatch, {"trailingEps": 3.2, "revenueGrowth": 0.08})
        flags = self.s._fallback_flags("X")
        assert flags is not None and flags["rev_reaccelerating"] is True

    def test_strong_earnings_growth(self, monkeypatch):
        self._patch_info(monkeypatch, {"trailingEps": -0.5, "earningsGrowth": 0.4})
        flags = self.s._fallback_flags("X")
        assert flags is not None and flags["eps_accelerating"] is True

    def test_high_revenue_growth(self, monkeypatch):
        self._patch_info(monkeypatch, {"revenueGrowth": 0.15})
        flags = self.s._fallback_flags("X")
        assert flags is not None and flags["rev_reaccelerating"] is True

    def test_est_revisions_from_margin(self, monkeypatch):
        self._patch_info(monkeypatch, {"trailingEps": 2.0, "grossMargins": 0.5})
        flags = self.s._fallback_flags("X")
        assert flags is not None and flags["est_revisions_up"] is True

    def test_declining_unprofitable_returns_none(self, monkeypatch):
        self._patch_info(monkeypatch, {
            "trailingEps": -1.0, "revenueGrowth": -0.05,
            "earningsGrowth": -0.2, "grossMargins": 0.1,
        })
        assert self.s._fallback_flags("X") is None

    def test_empty_info_returns_none(self, monkeypatch):
        self._patch_info(monkeypatch, {})
        assert self.s._fallback_flags("X") is None


# ── Integration: screen() returns list[dict] ──────────────────────────────────
class TestFundamentalScreenIntegration:
    def test_screen_empty_tickers(self):
        s = FundamentalScreen()
        s._use_fmp = False  # avoid the capability network probe
        assert s.screen([]) == []

    def test_screen_routes_to_fallback_when_no_key(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = ""
        s._use_fmp = False  # resolved (no key)
        calls = {"fallback": 0, "fmp": 0}
        flags = {"eps_accelerating": True, "rev_reaccelerating": False, "est_revisions_up": False}
        monkeypatch.setattr(s, "_fallback_flags", lambda t: calls.__setitem__("fallback", calls["fallback"] + 1) or flags)
        monkeypatch.setattr(s, "_fmp_flags", lambda t: calls.__setitem__("fmp", calls["fmp"] + 1) or flags)
        result = s.screen(["AAPL", "MSFT"])
        assert [d["ticker"] for d in result] == ["AAPL", "MSFT"]
        assert result[0]["eps_accelerating"] is True
        assert calls["fallback"] == 2 and calls["fmp"] == 0

    def test_screen_routes_to_fmp_when_key_set(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = "test-key"
        s._use_fmp = True  # resolved (key has fundamentals access)
        calls = {"fallback": 0, "fmp": 0}
        flags = {"eps_accelerating": True, "rev_reaccelerating": False, "est_revisions_up": False}
        monkeypatch.setattr(s, "_fallback_flags", lambda t: calls.__setitem__("fallback", calls["fallback"] + 1) or flags)
        monkeypatch.setattr(s, "_fmp_flags", lambda t: calls.__setitem__("fmp", calls["fmp"] + 1) or flags)
        s.screen(["AAPL"])
        assert calls["fmp"] == 1 and calls["fallback"] == 0

    def test_screen_skips_failing_and_survives_errors(self, monkeypatch):
        s = FundamentalScreen()
        s._fmp_key = ""
        flags = {"eps_accelerating": True, "rev_reaccelerating": False, "est_revisions_up": False}

        def _f(t):
            if t == "BAD":
                raise RuntimeError("data error")
            if t == "FAIL":
                return None  # doesn't pass — excluded
            return flags

        monkeypatch.setattr(s, "_fallback_flags", _f)
        result = s.screen(["GOOD", "BAD", "FAIL", "GOOD2"])
        assert [d["ticker"] for d in result] == ["GOOD", "GOOD2"]
