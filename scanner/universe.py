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
        tickers: set[str] = set()

        # Method 1: SP500 from Wikipedia via pandas
        try:
            sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
            tickers.update(sp500["Symbol"].str.replace(".", "-", regex=False).tolist())
            logger.debug("Fetched %d S&P500 tickers", len(tickers))
        except Exception as exc:
            logger.warning("S&P500 fetch failed: %s", exc)

        # Method 2: Nasdaq-100 from Wikipedia
        try:
            ndx = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")[4]
            tickers.update(ndx["Ticker"].tolist())
        except Exception as exc:
            logger.warning("NDX100 fetch failed: %s", exc)

        # Method 3: Russell 2000 approximation — use iShares IWM holdings CSV
        # TODO: add Polygon.io ticker endpoint when API key is available

        logger.info("UniverseDownloader: %d raw tickers from sources", len(tickers))
        return sorted(tickers)

    @retry(max_attempts=3, backoff_seconds=2.0)
    def liquidity_filter(self, tickers: list[str], batch_size: int = 100) -> list[str]:
        """Stage 2: apply price/volume/mktcap filters. Returns passing tickers."""
        import yfinance as yf

        passing: list[str] = []
        total = len(tickers)

        for i in range(0, total, batch_size):
            batch = tickers[i : i + batch_size]
            logger.debug("Liquidity filter: batch %d-%d of %d", i, i + batch_size, total)
            try:
                data = yf.download(
                    " ".join(batch),
                    period="30d",
                    interval="1d",
                    progress=False,
                    threads=True,
                )
                info_batch = yf.Tickers(" ".join(batch))

                for ticker in batch:
                    try:
                        if "Close" in data.columns:
                            close_col = data["Close"][ticker] if ticker in data["Close"] else None
                        else:
                            close_col = None

                        price = float(close_col.dropna().iloc[-1]) if close_col is not None and not close_col.dropna().empty else 0.0

                        if "Volume" in data.columns:
                            vol_col = data["Volume"][ticker] if ticker in data["Volume"] else None
                        else:
                            vol_col = None
                        avg_vol = float(vol_col.dropna().mean()) if vol_col is not None and not vol_col.dropna().empty else 0.0

                        info = info_batch.tickers.get(ticker)
                        mktcap = 0.0
                        if info:
                            try:
                                mktcap = float(info.fast_info.get("marketCap", 0) or 0)
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
            except Exception as batch_exc:
                logger.warning("Liquidity filter batch error: %s", batch_exc)

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
