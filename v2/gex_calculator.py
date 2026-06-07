"""V2: Net gamma exposure (GEX) calculator (Days 19-25).

GEX = sum over all strikes:
  gamma * open_interest * 100 * spot_price^2 * 0.01

For calls: positive GEX (market makers long gamma → pin strikes)
For puts: negative GEX (market makers short gamma → volatility amplifier)

Data source: Polygon.io options chain (requires API key)
TODO: POLYGON_API_KEY must be set in .env to use live GEX data
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import retry

logger = get_logger(__name__)


class GEXResult:
    """Immutable GEX computation result."""

    __slots__ = ("ticker", "net_gex", "call_gex", "put_gex", "largest_strike", "zero_gamma_level")

    def __init__(
        self,
        ticker: str,
        net_gex: float,
        call_gex: float,
        put_gex: float,
        largest_strike: Optional[float] = None,
        zero_gamma_level: Optional[float] = None,
    ) -> None:
        self.ticker = ticker
        self.net_gex = net_gex
        self.call_gex = call_gex
        self.put_gex = put_gex
        self.largest_strike = largest_strike
        self.zero_gamma_level = zero_gamma_level

    def __repr__(self) -> str:
        return (
            f"GEXResult(ticker={self.ticker!r}, net_gex={self.net_gex:.2e}, "
            f"largest_strike={self.largest_strike}, zero_gamma={self.zero_gamma_level})"
        )


class GEXCalculator:
    """Compute net gamma exposure from Polygon.io options chain."""

    def __init__(self) -> None:
        self._polygon_key = settings.POLYGON_API_KEY
        if not self._polygon_key:
            logger.warning(
                "GEXCalculator: POLYGON_API_KEY not set — "
                "TODO: set in .env to enable live GEX. Using mock data."
            )

    @retry(max_attempts=3, backoff_seconds=2.0)
    def _fetch_chain(self, ticker: str, expiry: Optional[str] = None) -> list[dict]:
        """Fetch options chain from Polygon.io REST API."""
        if not self._polygon_key:
            raise DataError("POLYGON_API_KEY not configured — GEX requires Polygon.io Starter plan")

        try:
            import requests

            params: dict = {
                "underlying_ticker": ticker,
                "limit": 250,
                "apiKey": self._polygon_key,
            }
            if expiry:
                params["expiration_date"] = expiry

            url = "https://api.polygon.io/v3/snapshot/options/" + ticker
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return list(data.get("results", []))  # type: ignore[return-value]
        except ImportError:
            raise DataError("requests library not available")
        except Exception as exc:
            raise DataError(f"Polygon options chain fetch failed: {exc}") from exc

    def _compute_contract_gex(
        self,
        gamma: float,
        open_interest: int,
        spot: float,
        option_type: str,
    ) -> float:
        """GEX for a single contract.

        Formula: gamma * OI * 100 * spot^2 * 0.01
        Sign: +1 for calls (MMs long gamma), -1 for puts (MMs short gamma)
        """
        sign = 1 if option_type.lower() in ("call", "c") else -1
        return sign * gamma * open_interest * 100 * (spot ** 2) * 0.01

    def compute(self, ticker: str, spot_price: float, expiry: Optional[str] = None) -> GEXResult:
        """Compute net GEX for a ticker.

        Args:
            ticker: Underlying symbol (e.g. "SPY")
            spot_price: Current spot price
            expiry: Optional ISO date string (e.g. "2024-01-19"). None = all expirations.

        Returns:
            GEXResult with net, call, put GEX and key levels
        """
        try:
            contracts = self._fetch_chain(ticker, expiry)
        except DataError as exc:
            logger.warning("GEXCalculator.compute: %s — returning zero GEX", exc)
            return GEXResult(ticker=ticker, net_gex=0.0, call_gex=0.0, put_gex=0.0)

        call_gex = 0.0
        put_gex = 0.0
        gex_by_strike: dict[float, float] = {}

        for contract in contracts:
            greeks = contract.get("greeks", {})
            gamma = float(greeks.get("gamma", 0) or 0)
            oi = int(contract.get("open_interest", 0) or 0)
            opt_type = contract.get("details", {}).get("contract_type", "call")
            strike = float(contract.get("details", {}).get("strike_price", 0) or 0)

            if gamma <= 0 or oi <= 0 or strike <= 0:
                continue

            gex = self._compute_contract_gex(gamma, oi, spot_price, opt_type)

            if opt_type.lower() in ("call", "c"):
                call_gex += gex
            else:
                put_gex += gex

            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex

        net_gex = call_gex + put_gex

        # Largest absolute GEX strike = likely pin/magnet level
        largest_strike = None
        if gex_by_strike:
            largest_strike = max(gex_by_strike, key=lambda k: abs(gex_by_strike[k]))

        # Zero-gamma level = strike where net flips from positive to negative
        zero_gamma_level = self._find_zero_gamma(gex_by_strike, spot_price)

        logger.debug(
            "GEX[%s]: net=%.2e call=%.2e put=%.2e largest_strike=%s zero_gamma=%s",
            ticker, net_gex, call_gex, put_gex, largest_strike, zero_gamma_level,
        )

        return GEXResult(
            ticker=ticker,
            net_gex=net_gex,
            call_gex=call_gex,
            put_gex=put_gex,
            largest_strike=largest_strike,
            zero_gamma_level=zero_gamma_level,
        )

    def _find_zero_gamma(
        self, gex_by_strike: dict[float, float], spot: float
    ) -> Optional[float]:
        """Find strike where cumulative GEX (from low to high) crosses zero."""
        if not gex_by_strike:
            return None
        strikes = sorted(gex_by_strike.keys())
        cumulative = 0.0
        prev_strike = None
        for strike in strikes:
            cumulative += gex_by_strike[strike]
            if prev_strike is not None and cumulative >= 0:
                # Crossed zero between prev_strike and this strike
                return float(prev_strike + (strike - prev_strike) / 2)
            if cumulative >= 0:
                prev_strike = strike
        return None

    def compute_offline(
        self,
        ticker: str,
        spot_price: float,
        contracts: list[dict],
    ) -> GEXResult:
        """Compute GEX from pre-fetched contract data (for testing / backtest)."""
        call_gex = 0.0
        put_gex = 0.0
        gex_by_strike: dict[float, float] = {}

        for c in contracts:
            gamma = float(c.get("gamma", 0))
            oi = int(c.get("open_interest", 0))
            opt_type = c.get("option_type", "call")
            strike = float(c.get("strike", 0))

            if strike <= 0:
                continue

            gex = self._compute_contract_gex(gamma, oi, spot_price, opt_type)
            if opt_type.lower() in ("call", "c"):
                call_gex += gex
            else:
                put_gex += gex
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex

        net_gex = call_gex + put_gex
        largest_strike = max(gex_by_strike, key=lambda k: abs(gex_by_strike[k])) if gex_by_strike else None
        zero_gamma = self._find_zero_gamma(gex_by_strike, spot_price)

        return GEXResult(
            ticker=ticker,
            net_gex=net_gex,
            call_gex=call_gex,
            put_gex=put_gex,
            largest_strike=largest_strike,
            zero_gamma_level=zero_gamma,
        )


__all__ = ["GEXCalculator", "GEXResult"]
