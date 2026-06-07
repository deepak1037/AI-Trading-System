"""Scanner Stage 1-2: Download universe + liquidity filter (Days 13-18).

Stage 1: Download ~5,800 NYSE + Nasdaq + AMEX tickers
Stage 2: Apply liquidity filter:
  - price > SCANNER_UNIVERSE_MIN_PRICE ($5)
  - avg 30-day volume > SCANNER_UNIVERSE_MIN_VOLUME (500k)
  - market cap > SCANNER_UNIVERSE_MIN_MKTCAP ($200M)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config.settings import settings
from core.logger import get_logger
from core.retry import retry

logger = get_logger(__name__)


class UniverseDownloader:
    """Stage 1-2: download and liquidity-filter the full stock universe."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or settings.DB_PATH

    def get_all_tickers(self) -> list[str]:
        """Stage 1: return NYSE+Nasdaq+AMEX tickers via yfinance screener."""
        try:
            import yfinance as yf

            # yfinance has a built-in screener via Tickers for common indices
            # Use a known list from yfinance's market info to get US exchange tickers
            from yfinance.screener.screener_query import EquityQuery  # type: ignore[import-untyped]

            # Build an "all US stocks" query — currently unused, fallback runs below
            EquityQuery("and", [
                EquityQuery("gt", ["price", 0]),
                EquityQuery("is", ["exchange", "NMS", "NYQ", "ASE"]),
            ])
            screener = yf.Screener()
            screener.set_predefined_body("most_actives")
            # This only returns ~25 — fallback to sector ETF holdings
        except Exception:
            pass

        # Primary method: scrape common index ETF holdings via yfinance
        return self._get_tickers_from_sources()

    def _get_tickers_from_sources(self) -> list[str]:
        """Fetch ticker list from multiple reliable sources."""
        import requests

        tickers: set[str] = set()
        session = requests.Session()
        session.headers["User-Agent"] = (
            "AI-Trading-System/1.0 (research; contact: trading@example.com)"
        )

        # ── Method 1: NASDAQ Trader FTP — official exchange listings ────────
        # nasdaqlisted.txt  → all NASDAQ-listed stocks
        # otherlisted.txt   → NYSE / AMEX / other exchange stocks
        for url, label in [
            ("http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",  "NASDAQ"),
            ("http://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",   "Other exchanges"),
        ]:
            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                df = pd.read_csv(
                    pd.io.common.StringIO(resp.text),
                    sep="|",
                    dtype=str,
                )
                # nasdaqlisted: Symbol column; otherlisted: ACT Symbol column
                col = "Symbol" if "Symbol" in df.columns else "ACT Symbol"
                syms = (
                    df[col]
                    .dropna()
                    .str.strip()
                    .replace(".", "-", regex=False)
                )
                # Drop test symbols, warrants, units, preferred (contain $ ^ /)
                valid = [s for s in syms if s and not any(c in s for c in "$^/~")]
                # Last row is a file-creation-date trailer — filter non-alpha starts
                valid = [s for s in valid if s[0].isalpha()]
                tickers.update(valid)
                logger.debug("NASDAQ Trader FTP (%s): %d tickers", label, len(valid))
            except Exception as exc:
                logger.warning("NASDAQ FTP %s fetch failed: %s", label, exc)

        # ── Method 2: SEC EDGAR company_tickers — all SEC-registered US equities
        try:
            resp = session.get(
                "https://www.sec.gov/files/company_tickers.json",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            syms = [
                v["ticker"].replace(".", "-")
                for v in data.values()
                if v.get("ticker") and v["ticker"][0].isalpha()
                and not any(c in v["ticker"] for c in "$^/~")
            ]
            tickers.update(syms)
            logger.debug("SEC EDGAR company_tickers: %d tickers", len(syms))
        except Exception as exc:
            logger.warning("SEC EDGAR fetch failed: %s", exc)

        # ── Method 3: S&P 500 from GitHub datasets CSV (always up-to-date) ──
        try:
            resp = session.get(
                "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
                "/main/data/constituents.csv",
                timeout=10,
            )
            resp.raise_for_status()
            sp500 = pd.read_csv(pd.io.common.StringIO(resp.text))
            syms = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
            tickers.update(syms)
            logger.debug("GitHub S&P500 CSV: %d tickers", len(syms))
        except Exception as exc:
            logger.warning("GitHub S&P500 CSV fetch failed: %s", exc)

        # ── Method 4: GitHub S&P 500 from GitHub datasets CSV ────────────────
        # (already fetched above as Method 3, this keeps the old name consistent)

        # ── Method 5: Wikipedia S&P500 (fallback with proper User-Agent) ────
        if len(tickers) < 100:
            for url, tbl_idx, col, label in [
                (
                    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                    0, "Symbol", "Wikipedia S&P500",
                ),
                (
                    "https://en.wikipedia.org/wiki/Nasdaq-100",
                    4, "Ticker", "Wikipedia NDX100",
                ),
            ]:
                try:
                    resp = session.get(url, timeout=10)
                    resp.raise_for_status()
                    df = pd.read_html(pd.io.common.StringIO(resp.text))[tbl_idx]
                    syms = df[col].str.replace(".", "-", regex=False).tolist()
                    tickers.update(syms)
                    logger.debug("%s: %d tickers", label, len(syms))
                except Exception as exc:
                    logger.warning("%s fetch failed: %s", label, exc)

        logger.info("UniverseDownloader: %d raw tickers from sources", len(tickers))
        return sorted(tickers)

    def liquidity_filter(self, tickers: list[str], batch_size: int = 25) -> list[str]:
        """Stage 2: apply price/volume/mktcap filters. Returns passing tickers.

        Uses small batches with inter-batch sleep to respect yfinance rate limits.
        Batch size 25 + 1.5s sleep keeps requests well under Yahoo's ~2 req/s limit.
        """
        import time

        import yfinance as yf

        passing: list[str] = []
        total = len(tickers)
        i = 0
        consecutive_rate_limits = 0

        while i < total:
            batch = tickers[i : i + batch_size]
            pct = (i / total * 100) if total else 0
            logger.info(
                "Liquidity filter: batch %d-%d of %d (%.0f%%)",
                i + 1, min(i + batch_size, total), total, pct,
            )
            try:
                data = yf.download(
                    " ".join(batch),
                    period="30d",
                    interval="1d",
                    progress=False,
                    threads=False,  # serial downloads respect rate limits better
                )
                consecutive_rate_limits = 0

                # yfinance returns MultiIndex columns when >1 ticker
                close_df = None
                vol_df = None
                if isinstance(data.columns, pd.MultiIndex):
                    if "Close" in data.columns.get_level_values(0):
                        close_df = data["Close"]
                    if "Volume" in data.columns.get_level_values(0):
                        vol_df = data["Volume"]
                else:
                    # Single ticker — wrap as DataFrame
                    if "Close" in data.columns:
                        close_df = data[["Close"]].rename(columns={"Close": batch[0]})
                    if "Volume" in data.columns:
                        vol_df = data[["Volume"]].rename(columns={"Volume": batch[0]})

                for ticker in batch:
                    try:
                        price = 0.0
                        avg_vol = 0.0
                        if close_df is not None and ticker in close_df.columns:
                            col = close_df[ticker].dropna()
                            if not col.empty:
                                price = float(col.iloc[-1])
                        if vol_df is not None and ticker in vol_df.columns:
                            col = vol_df[ticker].dropna()
                            if not col.empty:
                                avg_vol = float(col.mean())

                        # Market cap from fast_info (cached; no extra HTTP request)
                        mktcap = 0.0
                        try:
                            fi = yf.Ticker(ticker).fast_info
                            mktcap = float(getattr(fi, "market_cap", None) or 0)
                        except Exception:
                            mktcap = 0.0

                        if (
                            price >= settings.SCANNER_UNIVERSE_MIN_PRICE
                            and avg_vol >= settings.SCANNER_UNIVERSE_MIN_VOLUME
                            and mktcap >= settings.SCANNER_UNIVERSE_MIN_MKTCAP
                        ):
                            passing.append(ticker)
                    except Exception as ticker_exc:
                        logger.debug("Liquidity filter skip %s: %s", ticker, ticker_exc)

                i += batch_size
                # Respect rate limit: sleep between every batch
                if i < total:
                    time.sleep(1.5)

            except Exception as batch_exc:
                err = str(batch_exc)
                if "RateLimit" in err or "Too Many Requests" in err or "rate limit" in err.lower():
                    consecutive_rate_limits += 1
                    wait = min(30 * consecutive_rate_limits, 120)
                    logger.warning(
                        "yfinance rate limited. Waiting %ds before retrying batch %d...",
                        wait, i,
                    )
                    time.sleep(wait)
                    # Do NOT advance i — retry same batch
                else:
                    logger.warning("Liquidity filter batch error: %s", batch_exc)
                    i += batch_size  # skip broken batch, continue

        logger.info("UniverseDownloader: %d/%d tickers pass liquidity filter", len(passing), total)
        return passing

    def log_run(self, stage: int, tickers_in: int, tickers_out: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO scanner_runs (stage, tickers_in, tickers_out, run_at) VALUES (?,?,?,?)",
                (stage, tickers_in, tickers_out, datetime.now(tz=timezone.utc).isoformat()),
            )

    def run(self) -> list[str]:
        """Run Stage 1 + Stage 2 and return filtered tickers."""
        stage1 = self.get_all_tickers()
        self.log_run(1, 0, len(stage1))
        logger.info("Stage 1 complete: %d tickers", len(stage1))

        stage2 = self.liquidity_filter(stage1)
        self.log_run(2, len(stage1), len(stage2))
        logger.info("Stage 2 complete: %d tickers pass liquidity filter", len(stage2))
        return stage2


__all__ = ["UniverseDownloader"]
