"""Portfolio heat map — sector, delta, vega, expiry, and correlation exposure.

Generates a HeatMapReport from the open positions tracked by PositionWatcher
(or any dict list with the same shape). Runs on demand; the daily briefing
and risk dashboard call it at startup and on refresh.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# ── Sector mapping (watchlist tickers) ───────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "GOOG": "Technology", "META": "Technology", "CRWD": "Technology",
    "DDOG": "Technology", "FTNT": "Technology", "CSCO": "Technology",
    "IBM": "Technology", "ACN": "Technology", "NOW": "Technology",
    "CRM": "Technology", "ADBE": "Technology", "ORCL": "Technology",
    # Semiconductors (sub-sector — extra correlation)
    "NVDA": "Semiconductors", "AMD": "Semiconductors",
    "INTC": "Semiconductors", "KLAC": "Semiconductors",
    "AMAT": "Semiconductors", "LRCX": "Semiconductors",
    "MCHP": "Semiconductors", "NXPI": "Semiconductors",
    "TXN": "Semiconductors", "ARM": "Semiconductors", "AVGO": "Semiconductors",
    # Healthcare
    "JNJ": "Healthcare", "LLY": "Healthcare", "AMGN": "Healthcare",
    "BIIB": "Healthcare", "DVA": "Healthcare", "UNH": "Healthcare",
    "ABBV": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare",
    # Energy
    "VLO": "Energy", "MPC": "Energy", "EOG": "Energy",
    "XOM": "Energy", "CVX": "Energy", "SLB": "Energy",
    # Materials
    "FCX": "Materials", "NEM": "Materials", "APD": "Materials",
    # Financials
    "IVZ": "Financials", "BEN": "Financials",
    "JPM": "Financials", "GS": "Financials", "MS": "Financials",
    "BAC": "Financials", "WFC": "Financials",
    # Consumer
    "ROST": "Consumer", "TJX": "Consumer", "MAR": "Consumer",
    "HLT": "Consumer", "MNST": "Consumer", "MCD": "Consumer",
    "SBUX": "Consumer", "NKE": "Consumer", "AMZN": "Consumer",
    # Industrials
    "CAT": "Industrials", "GWW": "Industrials", "HWM": "Industrials",
    "ROK": "Industrials", "IEX": "Industrials", "FDX": "Industrials",
    "UPS": "Industrials", "RTX": "Industrials",
    # Real Estate
    "EQIX": "Real Estate", "IRM": "Real Estate", "PLD": "Real Estate",
    "SPG": "Real Estate", "FRT": "Real Estate",
    # Utilities
    "EVRG": "Utilities", "AES": "Utilities", "PCG": "Utilities",
    "NEE": "Utilities", "SO": "Utilities",
    # Communications
    "TKO": "Communications", "DIS": "Communications",
    "NFLX": "Communications", "T": "Communications", "VZ": "Communications",
}

# Correlated clusters — move together in market stress
CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "Tech mega-cap": ["AAPL", "MSFT", "GOOGL", "META", "NVDA"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "KLAC", "AMAT", "LRCX"],
    "Cloud software": ["DDOG", "FTNT", "CRWD", "NOW", "CRM"],
    "Energy": ["VLO", "MPC", "EOG", "XOM", "CVX"],
    "REITs": ["EQIX", "IRM", "PLD", "SPG"],
    "Consumer discretionary": ["AMZN", "ROST", "TJX", "MAR", "HLT"],
}


class SectorExposure(BaseModel):
    sector: str
    tickers: list[str]
    exposure_pct: float
    positions: int
    warning: bool


class CorrelatedCluster(BaseModel):
    cluster_name: str
    tickers: list[str]
    combined_exposure_pct: float
    risk_note: str


class HeatMapReport(BaseModel):
    generated_at: datetime
    total_positions: int
    total_exposure: float

    sector_exposure: dict[str, SectorExposure]
    sector_concentration_pct: dict[str, float]
    max_sector_pct: float
    max_sector_name: str
    sector_warning: bool

    net_delta: float
    delta_direction: str          # "bullish" | "bearish" | "neutral"
    beta_weighted_delta: float

    net_vega: float
    vega_direction: str           # "long_vol" | "short_vol" | "neutral"

    expiry_buckets: dict[str, int]
    same_expiry_warning: bool

    correlated_clusters: list[CorrelatedCluster]

    cash_buffer_pct: float
    cash_buffer_warning: bool


def _get(pos: Any, key: str, default: Any = None) -> Any:
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


class PortfolioHeatMap:
    """Generates a portfolio-level heat map from open positions."""

    def __init__(self, paper_account: Any = None) -> None:
        self._paper = paper_account

    def generate(self, positions: list[Any]) -> HeatMapReport:
        """Build a HeatMapReport from a list of positions (dicts or BucketPosition)."""
        now = datetime.now(tz=UTC)
        total_exp = self._total_exposure(positions)
        sector_exp = self._sector_breakdown(positions, total_exp)
        net_delta = self._calculate_net_delta(positions)
        net_vega = self._calculate_net_vega(positions)
        expiry_buckets = self._expiry_buckets(positions)
        clusters = self._find_clusters(positions, total_exp)
        cash_pct = self._cash_buffer_pct()
        max_sector_name, max_sector_pct = self._max_sector(sector_exp)

        return HeatMapReport(
            generated_at=now,
            total_positions=len(positions),
            total_exposure=round(total_exp, 2),
            sector_exposure=sector_exp,
            sector_concentration_pct={k: round(v.exposure_pct, 2) for k, v in sector_exp.items()},
            max_sector_pct=round(max_sector_pct, 2),
            max_sector_name=max_sector_name,
            sector_warning=max_sector_pct > settings.SECTOR_WARNING_PCT,
            net_delta=round(net_delta, 4),
            delta_direction=self._delta_direction(net_delta),
            beta_weighted_delta=round(net_delta, 4),  # simplified: no beta lookup
            net_vega=round(net_vega, 4),
            vega_direction=self._vega_direction(net_vega),
            expiry_buckets=expiry_buckets,
            same_expiry_warning=max(expiry_buckets.values(), default=0) >= settings.SAME_EXPIRY_WARNING,
            correlated_clusters=clusters,
            cash_buffer_pct=round(cash_pct, 2),
            cash_buffer_warning=cash_pct < settings.CASH_BUFFER_WARNING_PCT,
        )

    # ── exposure ─────────────────────────────────────────────────────────────

    def _total_exposure(self, positions: list[Any]) -> float:
        total = 0.0
        for pos in positions:
            entry = abs(float(_get(pos, "entry_price", 0) or 0))
            qty = abs(int(_get(pos, "qty", 1) or 1))
            is_opt = _get(pos, "option_type") in ("put", "call")
            mult = settings.OPTIONS_CONTRACT_MULTIPLIER if is_opt else 1
            total += entry * qty * mult
        return total

    def _position_exposure(self, pos: Any, total: float) -> float:
        if total <= 0:
            return 0.0
        entry = abs(float(_get(pos, "entry_price", 0) or 0))
        qty = abs(int(_get(pos, "qty", 1) or 1))
        is_opt = _get(pos, "option_type") in ("put", "call")
        mult = settings.OPTIONS_CONTRACT_MULTIPLIER if is_opt else 1
        return entry * qty * mult / total * 100.0

    # ── sector breakdown ─────────────────────────────────────────────────────

    def _sector_breakdown(self, positions: list[Any], total: float) -> dict[str, SectorExposure]:
        sector_tickers: dict[str, list[str]] = {}
        sector_pct: dict[str, float] = {}

        for pos in positions:
            ticker = _get(pos, "ticker", "")
            sector = SECTOR_MAP.get(ticker, "Other")
            sector_tickers.setdefault(sector, [])
            if ticker not in sector_tickers[sector]:
                sector_tickers[sector].append(ticker)
            sector_pct[sector] = sector_pct.get(sector, 0.0) + self._position_exposure(pos, total)

        result = {}
        for sector, pct in sector_pct.items():
            result[sector] = SectorExposure(
                sector=sector,
                tickers=sector_tickers.get(sector, []),
                exposure_pct=round(pct, 2),
                positions=len(sector_tickers.get(sector, [])),
                warning=pct > settings.SECTOR_WARNING_PCT,
            )
        return result

    def _max_sector(self, sector_exp: dict[str, SectorExposure]) -> tuple[str, float]:
        if not sector_exp:
            return ("None", 0.0)
        top = max(sector_exp.values(), key=lambda s: s.exposure_pct)
        return (top.sector, top.exposure_pct)

    # ── delta ────────────────────────────────────────────────────────────────

    def _calculate_net_delta(self, positions: list[Any]) -> float:
        total = 0.0
        for pos in positions:
            opt_type = _get(pos, "option_type")
            pos_type = str(_get(pos, "position_type", "") or "")
            qty = int(_get(pos, "qty", 1) or 1)

            if opt_type in ("put", "call"):
                delta = float(_get(pos, "delta") or 0.0)
                is_short = "short" in pos_type
                # Short reverses sign of delta
                effective_delta = -delta if is_short else delta
                total += effective_delta * qty * settings.OPTIONS_CONTRACT_MULTIPLIER
            else:
                # Equity position: long = +1 delta per share, short = -1
                shares = qty if "long" in pos_type or "long" not in pos_type.lower() + "short" else -qty
                if "short" in pos_type:
                    shares = -abs(qty)
                total += float(shares)
        return total

    def _delta_direction(self, delta: float) -> str:
        if delta > 50:
            return "bullish"
        if delta < -50:
            return "bearish"
        return "neutral"

    # ── vega ─────────────────────────────────────────────────────────────────

    def _calculate_net_vega(self, positions: list[Any]) -> float:
        total = 0.0
        for pos in positions:
            vega = float(_get(pos, "vega") or 0.0)
            pos_type = str(_get(pos, "position_type", "") or "")
            qty = int(_get(pos, "qty", 1) or 1)
            is_short = "short" in pos_type
            effective = -vega if is_short else vega
            total += effective * qty
        return total

    def _vega_direction(self, vega: float) -> str:
        if vega > 0.1:
            return "long_vol"
        if vega < -0.1:
            return "short_vol"
        return "neutral"

    # ── expiry concentration ─────────────────────────────────────────────────

    def _expiry_buckets(self, positions: list[Any]) -> dict[str, int]:
        today = date.today()
        buckets: dict[str, int] = {}
        for pos in positions:
            expiry_str = _get(pos, "expiry")
            if not expiry_str:
                continue
            try:
                exp = date.fromisoformat(str(expiry_str))
            except (ValueError, TypeError):
                continue
            days = (exp - today).days
            if days < 0:
                continue
            if days <= 7:
                label = "this_week"
            elif days <= 14:
                label = "next_week"
            elif days <= 30:
                label = "this_month"
            elif days <= 60:
                label = "next_month"
            else:
                label = "far_dated"
            buckets[label] = buckets.get(label, 0) + 1
        return buckets

    # ── correlated clusters ──────────────────────────────────────────────────

    def _find_clusters(self, positions: list[Any], total: float) -> list[CorrelatedCluster]:
        held = {str(_get(p, "ticker", "")) for p in positions}
        clusters = []
        for name, members in CORRELATION_CLUSTERS.items():
            overlap = [t for t in members if t in held]
            if len(overlap) < 2:
                continue
            combined_pct = sum(
                self._position_exposure(p, total)
                for p in positions
                if str(_get(p, "ticker", "")) in overlap
            )
            clusters.append(CorrelatedCluster(
                cluster_name=name,
                tickers=overlap,
                combined_exposure_pct=round(combined_pct, 2),
                risk_note=f"If market drops, {', '.join(overlap)} likely fall together",
            ))
        return clusters

    # ── cash buffer ──────────────────────────────────────────────────────────

    def _cash_buffer_pct(self) -> float:
        if self._paper is None:
            return float(settings.cash_buffer_pct)
        try:
            state = self._paper.get_state()
            equity = float(state.equity or 0)
            cash = float(state.cash or 0)
            return cash / equity * 100.0 if equity > 0 else 0.0
        except Exception as exc:
            logger.debug("PortfolioHeatMap: cash lookup failed: %s", exc)
            return float(settings.cash_buffer_pct)


__all__ = [
    "PortfolioHeatMap",
    "HeatMapReport",
    "SectorExposure",
    "CorrelatedCluster",
    "SECTOR_MAP",
    "CORRELATION_CLUSTERS",
]
