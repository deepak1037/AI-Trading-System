"""Manual CSV fallback for upcoming-earnings IV data (Phase 3, Step 1).

Moomoo's earnings IV endpoints require permissions the OpenD gateway may not
expose, so the most reliable path is to export the "Upcoming Earnings" screen
from the Moomoo desktop app to a CSV and drop it here::

    broker_client/earnings/data/upcoming_earnings.csv

CSV headers match Moomoo's columns (case/spacing tolerant)::

    Ticker, Earnings Date, IV, Last IV Crush, Historical IV Crush,
    IV Rank, IV Percentile, Expected Move, Chg on Last Earnings,
    Chg on Historical Est, Forecast Revenue YoY, Forecast EPS YoY

Every cell is parsed defensively — a "%" suffix, blanks, "N/A", or "--" all
degrade to 0/None rather than raising, so one bad row never sinks the load.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from broker_client.earnings.models import EarningsEvent
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

# Map of canonical field → accepted header spellings (lower-cased, stripped).
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "ticker": ("ticker", "symbol", "code"),
    "earnings_date": ("earnings date", "earnings_date", "date", "report date"),
    "earnings_time": ("earnings time", "time", "session", "bmo/amc"),
    "iv_current": ("iv", "iv current", "current iv", "implied volatility"),
    "last_iv_crush": ("last iv crush", "last_iv_crush"),
    "hist_iv_crush": (
        "historical iv crush", "hist iv crush", "hist_iv_crush",
    ),
    "iv_rank": ("iv rank", "iv_rank", "ivrank"),
    "iv_percentile": ("iv percentile", "iv_percentile", "ivpercentile"),
    "expected_move": ("expected move", "expected_move", "exp move"),
    "last_earnings_move": (
        "chg on last earnings", "chg last earn", "last earnings move",
        "actual move last",
    ),
    "hist_earnings_move": (
        "chg on historical est", "chg hist est", "hist earnings move",
        "avg actual move",
    ),
    "forecast_revenue_yoy": (
        "forecast revenue yoy", "forecast_revenue_yoy", "rev yoy",
    ),
    "forecast_eps_yoy": ("forecast eps yoy", "forecast_eps_yoy", "eps yoy"),
    "options_volume": ("options volume", "option volume", "volume"),
    "open_interest": ("open interest", "oi"),
    "stock_price": ("price", "stock price", "last price"),
}


class ManualEarningsInput:
    """Loads upcoming-earnings events from a manually-exported Moomoo CSV."""

    def __init__(self, csv_path: str | None = None) -> None:
        self.csv_path = csv_path or settings.EARNINGS_CSV_PATH

    def exists(self) -> bool:
        return Path(self.csv_path).exists()

    def load(self) -> list[EarningsEvent]:
        """Return all parseable events from the CSV (``[]`` if absent/empty)."""
        path = Path(self.csv_path)
        if not path.exists():
            logger.debug("No manual earnings CSV at %s", self.csv_path)
            return []
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames is None:
                    return []
                colmap = self._build_colmap(reader.fieldnames)
                events: list[EarningsEvent] = []
                for row in reader:
                    event = self._row_to_event(row, colmap)
                    if event is not None:
                        events.append(event)
            logger.info("Loaded %d earnings events from %s", len(events), self.csv_path)
            return events
        except Exception as exc:
            logger.warning("Failed to read earnings CSV %s: %s", self.csv_path, exc)
            return []

    # ── parsing helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _build_colmap(fieldnames: list[str]) -> dict[str, str]:
        """Map canonical field → the actual CSV column header present."""
        normalized = {name.strip().lower(): name for name in fieldnames if name}
        colmap: dict[str, str] = {}
        for field, aliases in _HEADER_ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    colmap[field] = normalized[alias]
                    break
        return colmap

    def _row_to_event(
        self, row: dict[str, Any], colmap: dict[str, str]
    ) -> EarningsEvent | None:
        ticker = self._get_str(row, colmap, "ticker").upper()
        if not ticker:
            return None
        ed = self._parse_date(self._get_str(row, colmap, "earnings_date"))
        if ed is None:
            logger.debug("Skipping %s — unparseable earnings date", ticker)
            return None
        try:
            return EarningsEvent(
                ticker=ticker,
                earnings_date=ed,
                earnings_time=self._parse_time(
                    self._get_str(row, colmap, "earnings_time")
                ),
                iv_current=self._num(row, colmap, "iv_current"),
                iv_rank=int(round(self._num(row, colmap, "iv_rank"))),
                iv_percentile=int(round(self._num(row, colmap, "iv_percentile"))),
                last_iv_crush=self._num(row, colmap, "last_iv_crush"),
                hist_iv_crush=self._num(row, colmap, "hist_iv_crush"),
                expected_move=self._num(row, colmap, "expected_move"),
                last_earnings_move=self._num(row, colmap, "last_earnings_move"),
                hist_earnings_move=self._num(row, colmap, "hist_earnings_move"),
                forecast_revenue_yoy=self._opt_num(row, colmap, "forecast_revenue_yoy"),
                forecast_eps_yoy=self._opt_num(row, colmap, "forecast_eps_yoy"),
                options_volume=self._opt_int(row, colmap, "options_volume"),
                open_interest=self._opt_int(row, colmap, "open_interest"),
                stock_price=self._opt_num(row, colmap, "stock_price"),
                source="manual",
            )
        except Exception as exc:
            logger.warning("Skipping malformed earnings row for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _get_str(row: dict[str, Any], colmap: dict[str, str], field: str) -> str:
        col = colmap.get(field)
        if col is None:
            return ""
        val = row.get(col)
        return str(val).strip() if val is not None else ""

    @classmethod
    def _num(cls, row: dict[str, Any], colmap: dict[str, str], field: str) -> float:
        return cls._opt_num(row, colmap, field) or 0.0

    @classmethod
    def _opt_num(
        cls, row: dict[str, Any], colmap: dict[str, str], field: str
    ) -> float | None:
        raw = cls._get_str(row, colmap, field)
        if not raw or raw.upper() in ("N/A", "NA", "--", "-", "NONE"):
            return None
        cleaned = raw.replace("%", "").replace(",", "").replace("±", "").replace("+", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    @classmethod
    def _opt_int(
        cls, row: dict[str, Any], colmap: dict[str, str], field: str
    ) -> int | None:
        val = cls._opt_num(row, colmap, field)
        return int(round(val)) if val is not None else None

    @staticmethod
    def _parse_time(raw: str) -> str:
        """Normalize the earnings session to BMO/AMC (default AMC)."""
        t = raw.strip().upper()
        if t in ("BMO", "AMC"):
            return t
        if "PRE" in t or "BEFORE" in t or "OPEN" in t:
            return "BMO"
        if "POST" in t or "AFTER" in t or "CLOSE" in t:
            return "AMC"
        return "AMC"

    @staticmethod
    def _parse_date(raw: str) -> date | None:
        if not raw:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


__all__ = ["ManualEarningsInput"]
