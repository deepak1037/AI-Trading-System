"""Scanner Stage 4: Institutional accumulation screen (Days 13-18).

Filters ~400 fundamental stocks to ~120 by:
  - Institutional ownership (13F holdings — yfinance, since reverse per-ticker
    13F lookup across all filers is not feasible cheaply)
  - Form 4 insider buying clusters (edgartools / SEC EDGAR)
  - Short interest not excessive (yfinance shortRatio proxy)

Uses the ``edgartools`` library (Apache 2.0), imported as ``edgar``. SEC
requires a User-Agent identity, set from ``settings.EDGAR_IDENTITY``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class AccumulationScreen:
    """Stage 4: institutional accumulation + insider buying + short squeeze setup."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH
        self._edgar_available = self._check_edgar()

    def _check_edgar(self) -> bool:
        try:
            import edgar  # type: ignore[import-untyped]

            # SEC requires a declared identity (User-Agent) on every request.
            edgar.set_identity(settings.EDGAR_IDENTITY)
            return True
        except ImportError:
            logger.warning(
                "edgartools (import name 'edgar') not installed — Stage 4 using "
                "yfinance proxies only. Run: pip install edgartools"
            )
            return False
        except Exception as exc:  # noqa: BLE001 — bad identity, network, etc.
            logger.warning("edgar init failed (%s) — using yfinance proxies", exc)
            return False

    def _get_institutional_holders(self, ticker: str) -> dict:
        """Return institutional holder info from yfinance (proxy for 13F)."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            holders = t.institutional_holders
            if holders is None or holders.empty:
                return {"holder_count": 0, "pct_held": 0.0}
            pct_col = [c for c in holders.columns if "pct" in c.lower() or "%" in c.lower()]
            if pct_col:
                pct_held = float(holders[pct_col[0]].sum())
            else:
                pct_held = 0.0
            return {"holder_count": len(holders), "pct_held": pct_held}
        except Exception as exc:
            logger.debug("AccumulationScreen: holders fetch failed for %s: %s", ticker, exc)
            return {"holder_count": 0, "pct_held": 0.0}

    def _get_short_ratio(self, ticker: str) -> float:
        """Return short ratio (days to cover) from yfinance info."""
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            info = t.fast_info
            return float(getattr(info, "short_ratio", None) or 0.0)
        except Exception:
            return 0.0

    def _get_info_metrics(self, ticker: str) -> dict:
        """One ``.info`` fetch → short + institutional-ownership metrics.

        Returns ``short_ratio`` (days to cover), ``short_interest_pct`` (% of
        float, 0-100), and ``pct_held`` (heldPercentInstitutions, 0-1).
        """
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info or {}
            return {
                "short_ratio": float(info.get("shortRatio") or 0.0),
                "short_interest_pct": float(info.get("shortPercentOfFloat") or 0.0) * 100.0,
                "pct_held": float(info.get("heldPercentInstitutions") or 0.0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("AccumulationScreen: .info metrics failed for %s: %s", ticker, exc)
            return {}

    def _get_edgar_form4_buys(self, ticker: str, scan_filings: int = 8) -> int:
        """Count open-market insider *purchases* across recent Form 4 filings.

        Uses edgartools' parsed ``Form4.common_stock_purchases`` DataFrame
        (open-market buys, transaction code "P"). Scans the most recent
        ``scan_filings`` Form 4s — each ``.obj()`` is one HTTP fetch, so we cap
        it to keep the per-ticker cost bounded.
        """
        if not self._edgar_available:
            return 0
        try:
            from edgar import Company  # type: ignore[import-untyped]

            company = Company(ticker)
            filings = company.get_filings(form="4")
            if filings is None or len(filings) == 0:
                return 0
            buy_count = 0
            for filing in filings.head(scan_filings):
                try:
                    form4 = filing.obj()
                    purchases = getattr(form4, "common_stock_purchases", None)
                    if purchases is not None and not purchases.empty:
                        buy_count += len(purchases)
                except Exception:  # noqa: BLE001 — skip unparseable filing
                    continue
            return buy_count
        except Exception as exc:  # noqa: BLE001
            logger.debug("Stage4: Form4 fetch failed for %s: %s", ticker, exc)
            return 0

    def _accumulation_data(self, ticker: str) -> Optional[dict]:
        """Return accumulation data for a passing ticker, else None.

        Data: ``holder_count``, ``pct_held``, ``form4_buys``, ``short_ratio``,
        ``short_interest_pct``.

        Primary gate: meaningful institutional ownership and not heavily shorted.
        A Form 4 open-market insider-buying *cluster* (2+ purchases) can rescue a
        name that lacks institutional-ownership data. Insider buying is NOT a
        mandatory requirement — open-market buys are rare, so requiring them
        would filter out almost everything. The expensive edgar lookup runs only
        for names that fail the institutional gate, keeping cost bounded.
        """
        holders = self._get_institutional_holders(ticker)
        metrics = self._get_info_metrics(ticker)

        holder_count = holders["holder_count"]
        # Prefer heldPercentInstitutions (varies per stock); fall back to the
        # summed top-holder percentage from institutional_holders.
        pct_held = metrics.get("pct_held") or holders["pct_held"]
        short_ratio = metrics.get("short_ratio", 0.0)
        short_interest_pct = metrics.get("short_interest_pct", 0.0)

        inst_interest = holder_count >= 5 or pct_held >= 0.20
        short_ok = short_ratio < 20.0 or short_ratio == 0.0
        if not short_ok:
            return None

        form4_buys = 0
        if not inst_interest:
            # Rescue path: insider buying cluster (only query edgar when needed).
            form4_buys = self._get_edgar_form4_buys(ticker) if self._edgar_available else 0
            if form4_buys < 2:
                return None

        return {
            "holder_count": int(holder_count),
            "pct_held": float(pct_held),
            "form4_buys": int(form4_buys),
            "short_ratio": float(short_ratio),
            "short_interest_pct": float(short_interest_pct),
        }

    def _passes_accumulation(self, ticker: str) -> bool:
        """Bool gate (used by the LLM ROI analyzer). See ``_accumulation_data``."""
        return self._accumulation_data(ticker) is not None

    def screen(self, tickers: list[str]) -> list[dict]:
        """Run Stage 4 screening. Returns per-stock dicts for passing tickers::

            {ticker, holder_count, pct_held, form4_buys, short_ratio,
             short_interest_pct}
        """
        import time

        pace = settings.SCANNER_YF_PACE_SECONDS
        passing: list[dict] = []
        for ticker in tickers:
            if pace:
                time.sleep(pace)
            try:
                data = self._accumulation_data(ticker)
                if data is not None:
                    passing.append({"ticker": ticker, **data})
            except Exception as exc:
                logger.debug("Stage4: error for %s: %s", ticker, exc)

        logger.info("Stage 4 accumulation screen: %d/%d pass", len(passing), len(tickers))
        return passing

    def log_run(self, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (4, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )


__all__ = ["AccumulationScreen"]
