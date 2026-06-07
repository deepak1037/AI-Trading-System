"""Tests for v2/gex_calculator.py (Days 19-25)."""

from __future__ import annotations

import pytest
from v2.gex_calculator import GEXCalculator, GEXResult


def _make_contracts(
    spot: float = 450.0,
    strikes: list[float] | None = None,
    oi: int = 1000,
    gamma: float = 0.05,
) -> list[dict]:
    """Build synthetic options chain for testing."""
    strikes = strikes or [440, 445, 450, 455, 460]
    contracts = []
    for s in strikes:
        # Calls: positive GEX
        contracts.append({
            "strike": float(s),
            "option_type": "call",
            "gamma": gamma,
            "open_interest": oi,
        })
        # Puts: negative GEX
        contracts.append({
            "strike": float(s),
            "option_type": "put",
            "gamma": gamma,
            "open_interest": oi,
        })
    return contracts


class TestGEXCalculator:
    def setup_method(self):
        self.calc = GEXCalculator()
        self.spot = 450.0
        self.contracts = _make_contracts(self.spot)

    def test_compute_offline_returns_result(self):
        result = self.calc.compute_offline("SPY", self.spot, self.contracts)
        assert isinstance(result, GEXResult)
        assert result.ticker == "SPY"

    def test_net_gex_is_zero_for_equal_calls_puts(self):
        # Equal call/put OI with same gamma → net GEX = 0
        result = self.calc.compute_offline("SPY", self.spot, self.contracts)
        assert abs(result.net_gex) < 1e-6, f"Expected ~0 net GEX, got {result.net_gex}"

    def test_positive_gex_from_calls_only(self):
        calls_only = [c for c in self.contracts if c["option_type"] == "call"]
        result = self.calc.compute_offline("SPY", self.spot, calls_only)
        assert result.call_gex > 0
        assert result.put_gex == 0.0
        assert result.net_gex > 0

    def test_negative_gex_from_puts_only(self):
        puts_only = [c for c in self.contracts if c["option_type"] == "put"]
        result = self.calc.compute_offline("SPY", self.spot, puts_only)
        assert result.put_gex < 0
        assert result.call_gex == 0.0
        assert result.net_gex < 0

    def test_largest_strike_identified(self):
        result = self.calc.compute_offline("SPY", self.spot, self.contracts)
        # With equal OI, all strikes have equal |GEX| — result will be one of them
        assert result.largest_strike is not None
        assert result.largest_strike in [440, 445, 450, 455, 460]

    def test_empty_contracts(self):
        result = self.calc.compute_offline("SPY", self.spot, [])
        assert result.net_gex == 0.0
        assert result.call_gex == 0.0
        assert result.put_gex == 0.0
        assert result.largest_strike is None

    def test_zero_gamma_contracts_ignored(self):
        zero_gamma = [{"strike": 450.0, "option_type": "call", "gamma": 0.0, "open_interest": 1000}]
        result = self.calc.compute_offline("SPY", self.spot, zero_gamma)
        assert result.net_gex == 0.0

    def test_contract_gex_formula(self):
        # Single call: gamma=0.05, OI=100, spot=100
        # GEX = 0.05 * 100 * 100 * 100^2 * 0.01 = 50000
        gex = self.calc._compute_contract_gex(0.05, 100, 100.0, "call")
        assert gex == pytest.approx(50000.0)

    def test_put_gex_negative(self):
        gex = self.calc._compute_contract_gex(0.05, 100, 100.0, "put")
        assert gex == pytest.approx(-50000.0)

    def test_compute_no_api_key_returns_zero(self):
        """With no API key, compute() should return zero GEX gracefully."""
        calc = GEXCalculator()
        calc._polygon_key = ""
        result = calc.compute("SPY", 450.0)
        assert result.net_gex == 0.0


class TestGEXResult:
    def test_repr(self):
        r = GEXResult("SPY", 1e9, 2e9, -1e9, largest_strike=450.0)
        assert "SPY" in repr(r)

    def test_zero_gamma_level(self):
        # net GEX goes negative → zero-gamma search
        contracts = [
            {"strike": 440.0, "option_type": "put", "gamma": 0.1, "open_interest": 5000},
            {"strike": 460.0, "option_type": "call", "gamma": 0.05, "open_interest": 1000},
        ]
        calc = GEXCalculator()
        result = calc.compute_offline("SPY", 450.0, contracts)
        # Put-heavy: negative net GEX expected
        assert result.net_gex < 0
