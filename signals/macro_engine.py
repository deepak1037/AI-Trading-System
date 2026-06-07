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
import statistics
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.exceptions import DataError, SignalError
from core.logger import get_logger
from core.retry import circuit_breaker, retry
from signals.signal_schema import Direction, Signal

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


__all__ = ["MacroEngine"]
