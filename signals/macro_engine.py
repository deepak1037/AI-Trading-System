"""Macro surprise scorer — NFP/CPI/FOMC signals (Day 2).

Pulls the actual release from FRED, compares it to the consensus forecast
(stored in the DB or provided by caller), and returns a Signal with a
z-score-based confidence. Forecasts are sourced from the FRED "vintages"
endpoint where available; otherwise the caller supplies a forecast dict.

Supported release codes (FRED series IDs):
    NFP  → PAYEMS (total nonfarm payrolls, thousands)
    CPI  → CPIAUCSL (all-items CPI, index)
    CORE → CPILFESL (core CPI ex-food+energy)
    PCE  → PCEPI (personal consumption expenditure deflator)
    FOMC → (no FRED series; caller provides direction override)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from config.settings import settings
from core.exceptions import DataError, RateLimitError, SignalError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from signals.signal_schema import Direction, Signal

if TYPE_CHECKING:
    from signals.bls_connector import BLSConnector, BLSReading

_ET = ZoneInfo("America/New_York")

# Non-seasonally-adjusted FRED series for the YoY-correct fallback path. The
# reported headline/core YoY are built from NSA indices (matching the BLS NSA
# series), NOT the SA indices used by the legacy index-level path.
_FRED_YOY_SERIES: dict[str, str] = {
    "CPI": "CPIAUCNS",   # CPI-U all items, NSA
    "CORE": "CPILFENS",  # all items less food & energy, NSA
}


def expected_cpi_period(release_label: str) -> Optional[str]:
    """Parse a release label → BLS period ``"YYYY-MM"``.

    Accepts both ``"May 2026"`` and the already-normalised ``"2026-05"`` form
    (the .env may carry either). Used to gate the live signal: BLS serves the
    prior month until the 8:30 AM print, so we only score once the published
    period reaches this value. Returns None if unparseable (gate disabled).
    """
    label = (release_label or "").strip()
    for fmt in ("%B %Y", "%Y-%m", "%b %Y"):
        try:
            dt = datetime.strptime(label, fmt)
            return f"{dt.year}-{dt.month:02d}"
        except ValueError:
            continue
    logger.warning(
        "CPI_RELEASE_LABEL %r not parseable as '<Month> <Year>' or 'YYYY-MM' — "
        "release gate disabled", release_label,
    )
    return None

logger = get_logger(__name__)

# Map friendly release names to FRED series IDs
_SERIES: dict[str, str] = {
    "NFP": "PAYEMS",
    "CPI": "CPIAUCSL",
    "CORE": "CPILFESL",
    "PCE": "PCEPI",
    "UNEMPLOYMENT": "UNRATE",
}

# Approximate historical monthly standard deviations (used as σ denominator)
# when we can't compute from history (e.g. first-ever release).
_FALLBACK_STD: dict[str, float] = {
    "PAYEMS": 75.0,  # thousands of jobs
    "CPIAUCSL": 0.3,  # index points
    "CPILFESL": 0.2,
    "PCEPI": 0.2,
    "UNRATE": 0.2,
}


def _get_fred_client():  # type: ignore[return]
    """Lazy-import fredapi and return configured client."""
    try:
        from fredapi import Fred  # type: ignore[import-untyped]
    except ImportError as exc:
        raise DataError("fredapi not installed") from exc
    if not settings.FRED_API_KEY:
        # TODO: FRED_API_KEY not configured — add to .env
        raise DataError("FRED_API_KEY not set in config")
    return Fred(api_key=settings.FRED_API_KEY)


class MacroEngine:
    """Produces a Signal from the latest macro data release.

    Calculates a z-score: (actual - forecast) / historical_std, then maps
    the z-score magnitude to a confidence and the sign to a direction.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        # Reused BLS connector + per-series TTL cache so the 30s macro tick polls
        # BLS at most once per BLS_CACHE_TTL_SECONDS (anonymous limit is 25/day).
        self._bls: Optional[BLSConnector] = None  # lazily-created
        self._bls_cache: dict[str, tuple[float, BLSReading]] = {}  # series_id → (ts, reading)

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
    )
    def _fetch_series_latest(self, series_id: str) -> float:
        """Return the most-recent observation value for a FRED series."""
        fred = _get_fred_client()
        try:
            data = fred.get_series(series_id, limit=1)
            if data.empty:
                raise DataError(f"No data returned for series {series_id!r}")
            return float(data.iloc[-1])
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"FRED fetch failed for series {series_id!r}: {exc}"
            ) from exc

    def _fetch_historical_std(self, series_id: str, n_obs: int = 24) -> float:
        """Compute std of month-over-month changes over the last n_obs observations."""
        fred = _get_fred_client()
        try:
            data = fred.get_series(series_id, limit=n_obs + 1)
            if len(data) < 3:
                return _FALLBACK_STD.get(series_id, 1.0)
            changes = data.diff().dropna()
            std = float(changes.std())
            return std if std > 0 else _FALLBACK_STD.get(series_id, 1.0)
        except Exception:
            return _FALLBACK_STD.get(series_id, 1.0)

    def _score_to_direction_confidence(
        self, z_score: float, release_type: str
    ) -> tuple[Direction, int]:
        """Map signed z-score → (direction, confidence 0-100).

        Positive z-score = actual > forecast = bullish (long).
        Negative z-score = actual < forecast = bearish (short).
        Exception: CPI/CORE/PCE — positive surprise = higher inflation = bearish.
        """
        inflation_releases = {"CPI", "CORE", "PCE"}
        sign = -1 if release_type in inflation_releases else 1
        adjusted = z_score * sign

        abs_z = abs(z_score)
        if abs_z >= settings.MACRO_SURPRISE_CRITICAL:
            confidence = min(95, 80 + int((abs_z - settings.MACRO_SURPRISE_CRITICAL) * 10))
            direction: Direction = "strong_long" if adjusted > 0 else "strong_short"
        elif abs_z >= settings.MACRO_SURPRISE_MODERATE:
            confidence = 55 + int(
                (abs_z - settings.MACRO_SURPRISE_MODERATE)
                / (settings.MACRO_SURPRISE_CRITICAL - settings.MACRO_SURPRISE_MODERATE)
                * 25
            )
            direction = "long" if adjusted > 0 else "short"
        else:
            confidence = max(10, int(abs_z / settings.MACRO_SURPRISE_MODERATE * 50))
            direction = "neutral"

        return direction, min(100, confidence)

    def score_release(
        self,
        release_name: str,
        forecast: Optional[float] = None,
        actual: Optional[float] = None,
    ) -> Signal:
        """Produce a Signal for the given macro release.

        Args:
            release_name: One of NFP, CPI, CORE, PCE, UNEMPLOYMENT.
            forecast: Analyst consensus estimate. If None, fetches the
                prior observation from FRED as a naive proxy.
            actual: The released value. If None, fetches latest from FRED.

        Returns:
            Signal with source="macro".
        """
        release_upper = release_name.upper()
        series_id = _SERIES.get(release_upper)
        if series_id is None:
            raise SignalError(
                f"Unknown release: {release_name!r}. Valid: {list(_SERIES)}"
            )

        if actual is None:
            actual = self._fetch_series_latest(series_id)
            logger.debug("Fetched %s actual=%.4f", series_id, actual)

        if forecast is None:
            # Use the prior observation as a naive forecast proxy
            fred = _get_fred_client()
            try:
                data = fred.get_series(series_id, limit=2)
                forecast = float(data.iloc[-2]) if len(data) >= 2 else actual
            except Exception:
                forecast = actual
            logger.debug(
                "No forecast provided for %s; using prior observation=%.4f as proxy",
                release_name,
                forecast,
            )

        std = self._fetch_historical_std(series_id)
        if std == 0:
            std = _FALLBACK_STD.get(series_id, 1.0)

        z_score = (actual - forecast) / std
        direction, confidence = self._score_to_direction_confidence(z_score, release_upper)

        logger.info(
            "MacroEngine: release=%s actual=%.4f forecast=%.4f z=%.3f "
            "direction=%s confidence=%d",
            release_name,
            actual,
            forecast,
            z_score,
            direction,
            confidence,
        )

        return Signal(
            direction=direction,
            confidence=confidence,
            source="macro",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "release": release_name,
                "actual": actual,
                "forecast": forecast,
                "z_score": round(z_score, 4),
                "series_id": series_id,
            },
        )

    def score_release_offline(
        self,
        release_name: str,
        actual: float,
        forecast: float,
        historical_std: float,
        timestamp: Optional[datetime] = None,
    ) -> Signal:
        """Score a release from provided values (for backtesting / replay).

        No API calls are made. All values are supplied by the caller.
        """
        release_upper = release_name.upper()
        if release_upper not in _SERIES:
            raise SignalError(f"Unknown release: {release_name!r}")

        std = historical_std if historical_std > 0 else _FALLBACK_STD.get(
            _SERIES[release_upper], 1.0
        )
        z_score = (actual - forecast) / std
        direction, confidence = self._score_to_direction_confidence(z_score, release_upper)

        ts = timestamp or datetime.now(tz=timezone.utc)
        return Signal(
            direction=direction,
            confidence=confidence,
            source="macro",
            timestamp=ts,
            metadata={
                "release": release_name,
                "actual": actual,
                "forecast": forecast,
                "z_score": round(z_score, 4),
                "historical_std": historical_std,
                "mode": "offline",
            },
        )

    # ── BLS live path (CPI/CORE at release time, no FRED lag) ─────────────────

    # Map our release codes to (settings series-id attr, settings consensus attr).
    _BLS_RELEASES: dict[str, tuple[str, str]] = {
        "CPI": ("CPI_SERIES_ID", "CPI_CONSENSUS_YOY"),
        "CORE": ("CORE_CPI_SERIES_ID", "CORE_CPI_CONSENSUS_YOY"),
    }

    def score_release_bls(
        self,
        release_name: str,
        consensus: Optional[float] = None,
        current_year: Optional[int] = None,
    ) -> Signal:
        """Score a CPI release LIVE from the BLS API (YoY surprise vs consensus).

        Unlike :meth:`score_release` (FRED, index-level, lagged), this reads the
        BLS API directly — which reflects the print at 8:30 AM ET — and works in
        YoY-percent space so it can compare against the reported consensus.

        Args:
            release_name: ``"CPI"`` or ``"CORE"``.
            consensus: YoY-percent consensus. Defaults to the configured value.
            current_year: Release year (defaults to the current ET year). Passed
                in for deterministic tests.

        Raises:
            SignalError: unknown release.
            DataError: BLS fetch/compute failed (caller may fall back to FRED).
        """
        release_upper = release_name.upper()
        mapping = self._BLS_RELEASES.get(release_upper)
        if mapping is None:
            raise SignalError(
                f"BLS path supports CPI/CORE only, got {release_name!r}"
            )
        series_attr, consensus_attr = mapping
        series_id = getattr(settings, series_attr)
        if consensus is None:
            consensus = float(getattr(settings, consensus_attr))

        year = current_year or datetime.now(tz=_ET).year
        reading = self._get_bls_reading(series_id, year)

        z_score = (reading.yoy_pct - consensus) / reading.yoy_std
        direction, confidence = self._score_to_direction_confidence(
            z_score, release_upper
        )

        logger.info(
            "MacroEngine[BLS]: %s (%s) actual=%.2f%% consensus=%.2f%% "
            "z=%.3f → direction=%s confidence=%d",
            release_name, reading.period_label, reading.yoy_pct, consensus,
            z_score, direction, confidence,
        )

        return Signal(
            direction=direction,
            confidence=confidence,
            source="macro",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "release": release_name,
                "release_label": settings.CPI_RELEASE_LABEL,
                "period": reading.period_label,
                "actual_yoy": reading.yoy_pct,
                "consensus_yoy": consensus,
                "index_value": reading.index_value,
                "z_score": round(z_score, 4),
                "historical_std": reading.yoy_std,
                "series_id": series_id,
                "source_api": "BLS",
            },
        )

    def _get_bls_reading(self, series_id: str, year: int) -> BLSReading:
        """Return a BLS reading, served from a TTL cache to spare the BLS quota.

        On a rate-limit (RateLimitError) we do NOT retry or hammer BLS — if a
        cached reading exists (even stale) we serve it and wait for the next
        tick; otherwise the throttle propagates so the caller can fall back to
        FRED.
        """
        now = time.monotonic()
        cached = self._bls_cache.get(series_id)
        if cached is not None and (now - cached[0]) < settings.BLS_CACHE_TTL_SECONDS:
            return cached[1]

        if self._bls is None:
            from signals.bls_connector import BLSConnector
            self._bls = BLSConnector()
        try:
            reading = self._bls.latest_yoy(series_id, year)
        except RateLimitError:
            if cached is not None:
                logger.warning(
                    "BLS rate-limited for %s — serving cached reading (age %.0fs)",
                    series_id, now - cached[0],
                )
                return cached[1]
            raise
        self._bls_cache[series_id] = (now, reading)
        return reading

    def score_cpi_live(
        self, release_name: str, current_year: Optional[int] = None
    ) -> Signal:
        """BLS-first CPI scoring with automatic FRED fallback.

        Tries the live BLS YoY path; on a BLS rate-limit or any other failure,
        falls back to the YoY-correct FRED path so the tick still produces a
        macro signal in the SAME units (percent vs consensus), not a raw index.
        """
        try:
            return self.score_release_bls(release_name, current_year=current_year)
        except RateLimitError as exc:
            logger.warning(
                "MacroEngine: BLS rate-limited for %s (%s) — FRED YoY fallback",
                release_name, exc,
            )
            return self._score_release_fred_yoy(release_name)
        except (DataError, SignalError) as exc:
            logger.warning(
                "MacroEngine: BLS path failed for %s (%s) — FRED YoY fallback",
                release_name, exc,
            )
            return self._score_release_fred_yoy(release_name)

    def _score_release_fred_yoy(
        self, release_name: str, consensus: Optional[float] = None
    ) -> Signal:
        """FRED fallback that computes YoY% (not a raw index level).

        Bug fix: the legacy index-level path compared an index value (and a
        get_series(limit=1) call that returns the OLDEST observation, e.g. the
        1947 CPI of ~21.5) against a percent consensus. This path fetches the
        NSA series, computes ``yoy = (current/year_ago - 1) * 100`` from the
        latest 13 months, and compares to the percent consensus — identical
        units to the BLS path.
        """
        release_upper = release_name.upper()
        series_id = _FRED_YOY_SERIES.get(release_upper)
        if series_id is None:
            # Non-CPI release: no YoY consensus defined — use the legacy path.
            return self.score_release(release_name)

        consensus_attr = self._BLS_RELEASES[release_upper][1]
        if consensus is None:
            consensus = float(getattr(settings, consensus_attr))

        fred = _get_fred_client()
        try:
            data = fred.get_series(series_id).dropna()
        except Exception as exc:  # network/parse
            raise DataError(f"FRED YoY fetch failed for {series_id!r}: {exc}") from exc
        if len(data) < 13:
            raise DataError(
                f"FRED {series_id!r}: only {len(data)} obs — need 13 for YoY"
            )

        current = float(data.iloc[-1])
        year_ago = float(data.iloc[-13])
        if year_ago == 0:
            raise DataError(f"FRED {series_id!r}: zero year-ago value")
        yoy_pct = (current / year_ago - 1.0) * 100.0
        period_label = data.index[-1].strftime("%Y-%m")

        # Surprise sigma = std of MoM changes in the YoY rate over history.
        yoy_hist = [
            (float(data.iloc[i]) / float(data.iloc[i - 12]) - 1.0) * 100.0
            for i in range(12, len(data))
            if float(data.iloc[i - 12]) != 0
        ]
        std = settings.CPI_YOY_FALLBACK_STD
        if len(yoy_hist) >= 4:
            changes = [yoy_hist[i] - yoy_hist[i - 1] for i in range(1, len(yoy_hist))]
            mean = sum(changes) / len(changes)
            var = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
            computed = var**0.5
            if computed > 0:
                std = computed

        z_score = (yoy_pct - consensus) / std
        direction, confidence = self._score_to_direction_confidence(
            z_score, release_upper
        )
        logger.info(
            "MacroEngine[FRED-YoY]: %s (%s) actual=%.2f%% consensus=%.2f%% "
            "z=%.3f → direction=%s confidence=%d",
            release_name, period_label, yoy_pct, consensus,
            z_score, direction, confidence,
        )
        return Signal(
            direction=direction,
            confidence=confidence,
            source="macro",
            timestamp=datetime.now(tz=timezone.utc),
            metadata={
                "release": release_name,
                "release_label": settings.CPI_RELEASE_LABEL,
                "period": period_label,
                "actual_yoy": round(yoy_pct, 3),
                "consensus_yoy": consensus,
                "index_value": round(current, 4),
                "z_score": round(z_score, 4),
                "historical_std": round(std, 4),
                "series_id": series_id,
                "source_api": "FRED",
            },
        )

    def log_signal_to_db(self, signal: Signal) -> int:
        """Persist signal to SQLite signals table. Returns the new row id."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """INSERT INTO signals (direction, confidence, source, timestamp, metadata_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    signal.direction,
                    signal.confidence,
                    signal.source,
                    signal.timestamp.isoformat(),
                    str(signal.metadata),
                ),
            )
            return cur.lastrowid or 0


__all__ = ["MacroEngine", "expected_cpi_period"]
