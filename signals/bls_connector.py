"""BLS (Bureau of Labor Statistics) Public API v2 connector — live CPI.

FRED mirrors the BLS release with a 15-30 minute lag. The BLS API itself
reflects the print the moment it is published (8:30 AM ET on release day), so
during the macro phase we read CPI here and only fall back to FRED if BLS fails.

The API returns the CPI as an *index level* (e.g. 313.5), NOT a year-over-year
percentage. The reported headline number markets react to is YoY, computed from
the NON-seasonally-adjusted index (series ``CUUR0000SA0``). So this module's job
is to fetch enough history to compute:

    yoy_pct = (index[this month] / index[same month, prior year] - 1) * 100

and a surprise sigma = std of month-over-month changes in that YoY rate.

Docs: https://www.bls.gov/developers/api_signature_v2.htm
No key is required for basic access (25 queries/day); a free registration key
raises the limit to 500/day and is sent when ``BLS_API_KEY`` is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from core.exceptions import DataError, RateLimitError
from core.logger import get_logger
from core.retry import circuit_breaker, retry

logger = get_logger(__name__)

# BLS period codes M01..M12 are the 12 months; M13 is the annual average (skip).
_MONTHLY_PERIODS = {f"M{m:02d}" for m in range(1, 13)}


@dataclass(frozen=True)
class BLSObservation:
    """A single monthly CPI index observation."""

    year: int
    period: str  # "M01".."M12"
    value: float  # index level (NOT a percentage)

    @property
    def month(self) -> int:
        return int(self.period[1:])

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


@dataclass(frozen=True)
class BLSReading:
    """Latest CPI reading with the derived YoY surprise inputs."""

    series_id: str
    period_label: str  # e.g. "2026-05"
    index_value: float  # latest index level
    yoy_pct: float  # latest year-over-year percent
    yoy_std: float  # surprise sigma: std of MoM changes in the YoY rate (pp)
    observations: list[BLSObservation] = field(default_factory=list)


class BLSConnector:
    """Fetches CPI series from the BLS Public API v2 and derives YoY."""

    def __init__(self, api_url: str | None = None, api_key: str | None = None) -> None:
        self._url = api_url or settings.BLS_API_URL
        self._key = api_key if api_key is not None else settings.BLS_API_KEY

    @retry(
        max_attempts=settings.API_MAX_RETRIES,
        backoff_seconds=settings.API_BACKOFF_SECONDS,
        exceptions=(DataError,),
    )
    @circuit_breaker(
        failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
        recovery_timeout=settings.API_CIRCUIT_BREAKER_TIMEOUT,
        expected_exceptions=(DataError,),  # a throttle must NOT trip the breaker
    )
    def fetch_series(
        self, series_id: str, start_year: int, end_year: int
    ) -> list[BLSObservation]:
        """Return monthly observations for ``series_id`` over [start, end], oldest first.

        Raises:
            RateLimitError: BLS throttled us (HTTP 429 or a daily-threshold
                message). NOT retried — the caller waits for the next tick.
            DataError: any other transport/parse/status failure (caller may
                fall back to FRED).
        """
        import requests  # local import: keep module import cheap

        payload: dict[str, Any] = {
            "seriesid": [series_id],
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        if self._key:
            payload["registrationkey"] = self._key

        try:
            resp = requests.post(
                self._url,
                json=payload,
                timeout=settings.LLM_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:  # transport failure → DataError (retriable)
            raise DataError(f"BLS request failed for {series_id!r}: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"BLS rate limit (HTTP 429) for {series_id!r}")

        try:
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            raise DataError(f"BLS request failed for {series_id!r}: {exc}") from exc

        if body.get("status") != "REQUEST_SUCCEEDED":
            msgs = "; ".join(body.get("message", []) or ["unknown error"])
            # BLS signals throttling in the message body even on a 200 response.
            low = msgs.lower()
            if "threshold" in low or "rate limit" in low or "exceeded" in low:
                raise RateLimitError(f"BLS rate limit for {series_id!r}: {msgs}")
            raise DataError(f"BLS API error for {series_id!r}: {msgs}")

        try:
            rows = body["Results"]["series"][0]["data"]
        except (KeyError, IndexError) as exc:
            raise DataError(f"BLS response missing data for {series_id!r}") from exc

        obs: list[BLSObservation] = []
        for row in rows:
            period = row.get("period", "")
            if period not in _MONTHLY_PERIODS:
                continue  # skip M13 annual average / quarterlies
            try:
                obs.append(
                    BLSObservation(
                        year=int(row["year"]),
                        period=period,
                        value=float(row["value"]),
                    )
                )
            except (KeyError, ValueError):
                continue  # skip malformed / preliminary blank rows

        if not obs:
            raise DataError(f"BLS returned no monthly observations for {series_id!r}")

        # API returns newest-first; sort oldest-first for YoY math.
        obs.sort(key=lambda o: (o.year, o.month))
        return obs

    @staticmethod
    def _yoy_series(obs: list[BLSObservation]) -> list[float]:
        """Compute the YoY percent for every month that has a prior-year match."""
        by_key = {(o.year, o.month): o.value for o in obs}
        yoy: list[float] = []
        for o in obs:
            prior = by_key.get((o.year - 1, o.month))
            if prior and prior != 0:
                yoy.append((o.value / prior - 1.0) * 100.0)
        return yoy

    def latest_yoy(self, series_id: str, current_year: int) -> BLSReading:
        """Fetch and reduce ``series_id`` to its latest YoY reading + surprise sigma.

        Args:
            series_id: BLS series id (e.g. ``CUUR0000SA0``).
            current_year: The release year — we pull 3 calendar years ending here
                so there is enough history both for the YoY base and for the
                std-of-YoY-changes surprise sigma. Passed in (not derived from a
                clock) so this stays deterministic and testable.
        """
        obs = self.fetch_series(series_id, current_year - 2, current_year)
        latest = obs[-1]
        prior_year = next(
            (o for o in obs if o.year == latest.year - 1 and o.month == latest.month),
            None,
        )
        if prior_year is None or prior_year.value == 0:
            raise DataError(
                f"BLS {series_id!r}: no prior-year value for {latest.label} — "
                "cannot compute YoY"
            )
        yoy_pct = (latest.value / prior_year.value - 1.0) * 100.0

        yoy_hist = self._yoy_series(obs)
        if len(yoy_hist) >= 4:
            changes = [yoy_hist[i] - yoy_hist[i - 1] for i in range(1, len(yoy_hist))]
            mean = sum(changes) / len(changes)
            var = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
            std = var**0.5
        else:
            std = 0.0
        if std <= 0:
            std = settings.CPI_YOY_FALLBACK_STD

        logger.info(
            "BLS %s: %s index=%.3f yoy=%.2f%% sigma=%.3fpp (%d obs)",
            series_id, latest.label, latest.value, yoy_pct, std, len(obs),
        )
        return BLSReading(
            series_id=series_id,
            period_label=latest.label,
            index_value=latest.value,
            yoy_pct=round(yoy_pct, 3),
            yoy_std=round(std, 4),
            observations=obs,
        )


__all__ = ["BLSConnector", "BLSReading", "BLSObservation"]
