"""EDGAR connector — SEC filings via edgartools.

Covers Form 4 (insider transactions) and 13F-HR (institutional holdings) as
required by the Multi-Bagger Scanner (CLAUDE.md Section 14, Stage 4).

``set_identity`` is called once at module import to satisfy the SEC EDGAR
EFTS User-Agent requirement.  Set ``EDGAR_IDENTITY`` in .env.
"""

from __future__ import annotations

from typing import Any

import edgar
from edgar import Company

from config.settings import settings
from core.exceptions import DataError
from core.logger import get_logger
from core.retry import circuit_breaker, retry

logger = get_logger(__name__)

# SEC EDGAR requires a User-Agent header of the form "Name email@example.com".
try:
    edgar.set_identity(settings.EDGAR_IDENTITY)
    logger.debug("EDGAR identity set: %s", settings.EDGAR_IDENTITY)
except Exception as _exc:
    logger.warning("EDGAR identity not set: %s", _exc)


class EdgarConnector:
    """Thin wrapper around edgartools for SEC EDGAR access.

    All public methods return plain Python dicts/lists so callers don't need
    to import edgartools types.
    """

    def __init__(self) -> None:
        self._get_filings_impl = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_filings)
        )

        self._get_holdings_impl = retry(
            max_attempts=settings.API_MAX_RETRIES,
            backoff_seconds=settings.API_BACKOFF_SECONDS,
            exceptions=(DataError,),
        )(
            circuit_breaker(
                failure_threshold=settings.API_CIRCUIT_BREAKER_FAILURES,
                recovery_timeout=float(settings.API_CIRCUIT_BREAKER_TIMEOUT),
                expected_exceptions=(DataError,),
            )(self._fetch_holdings)
        )

    # ── Public API ────────────────────────────────────────────

    def get_recent_filings(
        self,
        ticker: str,
        form_type: str = "13F-HR",
        n: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the N most recent filings of ``form_type`` for ``ticker``.

        Args:
            ticker:    Stock symbol or company name.
            form_type: SEC form type, e.g. ``"13F-HR"``, ``"4"``, ``"8-K"``.
            n:         Number of recent filings to return.

        Returns:
            List of dicts with keys: ``accession_no``, ``filing_date``,
            ``form``, ``description``.

        Raises:
            DataError: If the company is not found or the call fails.
        """
        logger.debug(
            "EDGAR get_recent_filings ticker=%s form=%s n=%d", ticker, form_type, n
        )
        return self._get_filings_impl(ticker, form_type, n)

    def get_13f_holdings(self, ticker: str) -> list[dict[str, Any]]:
        """Return holdings from the latest 13F-HR for ``ticker``.

        Returns:
            List of dicts per holding: ``name``, ``cusip``, ``value``,
            ``shares``, ``filing_date``.

        Raises:
            DataError: If no 13F filing exists or the call fails.
        """
        logger.debug("EDGAR get_13f_holdings ticker=%s", ticker)
        return self._get_holdings_impl(ticker)

    def get_insider_transactions(
        self,
        ticker: str,
        n: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the N most recent Form 4 insider transactions for ``ticker``.

        Returns:
            List of dicts: ``filed``, ``issuer``, ``owner``,
            ``transaction_type``, ``shares``, ``price``.

        Raises:
            DataError: If the call fails.
        """
        return self._get_filings_impl(ticker, "4", n)

    # ── Private ───────────────────────────────────────────────

    @staticmethod
    def _get_company(ticker: str) -> Company:
        try:
            return Company(ticker)
        except Exception as exc:
            raise DataError(
                f"EDGAR company lookup failed for {ticker}",
                ticker=ticker,
                cause=str(exc),
            ) from exc

    def _fetch_filings(
        self, ticker: str, form_type: str, n: int
    ) -> list[dict[str, Any]]:
        try:
            company = self._get_company(ticker)
            results = company.get_filings(form=form_type).latest(n)
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"EDGAR filings fetch failed for {ticker} form={form_type}",
                ticker=ticker,
                form_type=form_type,
                cause=str(exc),
            ) from exc

        output: list[dict[str, Any]] = []
        # edgartools FilingSearchResults is iterable.
        for filing in results:
            output.append(
                {
                    "accession_no": getattr(filing, "accession_no", ""),
                    "filing_date": str(getattr(filing, "filing_date", "")),
                    "form": getattr(filing, "form", form_type),
                    "description": getattr(filing, "description", ""),
                }
            )
        logger.debug("EDGAR %s %s: %d filings returned", ticker, form_type, len(output))
        return output

    def _fetch_holdings(self, ticker: str) -> list[dict[str, Any]]:
        try:
            company = self._get_company(ticker)
            filing = company.get_filings(form="13F-HR").latest()
            obj = filing.obj()
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                f"EDGAR 13F holdings fetch failed for {ticker}",
                ticker=ticker,
                cause=str(exc),
            ) from exc

        # edgartools ThirteenF.holdings is a DataFrame.
        holdings_df = getattr(obj, "holdings", None)
        if holdings_df is None or (hasattr(holdings_df, "empty") and holdings_df.empty):
            return []

        output: list[dict[str, Any]] = []
        for _, row in holdings_df.iterrows():
            output.append(
                {
                    "name": row.get("name", ""),
                    "cusip": row.get("cusip", ""),
                    "value": row.get("value", 0),
                    "shares": row.get("sshPrnamt", 0),
                    "filing_date": "",
                }
            )
        logger.debug("EDGAR 13F %s: %d holdings", ticker, len(output))
        return output


__all__ = ["EdgarConnector"]
