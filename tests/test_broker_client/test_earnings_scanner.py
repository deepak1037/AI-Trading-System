"""Tests for the Phase 3 earnings scanner + CLI rendering."""

from __future__ import annotations

from datetime import date, timedelta

from broker_client.earnings import cli
from broker_client.earnings.earnings_scanner import EarningsScanner
from broker_client.earnings.iv_analyzer import IVHistoryAnalyzer
from broker_client.earnings.llm_assessor import LLMEarningsAssessor
from broker_client.earnings.models import (
    IV_CRUSH,
    IV_SPIKE,
    SKIP,
    EarningsEvent,
    EarningsIVHistory,
    IVAnalysis,
)
from broker_client.earnings.strategy_builder import EarningsStrategyBuilder


def _future(days: int) -> date:
    return date.today() + timedelta(days=days)


class _StubProvider:
    def __init__(self, events) -> None:
        self._events = events

    def get_upcoming_earnings(self, days_ahead=None, only_ticker=None):
        return self._events

    def get_ticker_earnings(self, ticker, days_ahead=None):
        for e in self._events:
            if e.ticker.upper() == ticker.upper():
                return e
        return None

    def get_iv_history(self, ticker):
        return EarningsIVHistory(ticker=ticker, quarters=[])  # force summary path


def _crush_event(ticker="NVDA", price=135.0, when=10) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker, earnings_date=_future(when), earnings_time="AMC",
        iv_rank=72, iv_percentile=70, hist_iv_crush=24.0, last_iv_crush=26.0,
        expected_move=8.0, last_earnings_move=4.0, stock_price=price,
    )


def _spike_event(ticker="TSLA", price=200.0, when=10) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker, earnings_date=_future(when), earnings_time="AMC",
        iv_rank=20, iv_percentile=30, hist_iv_crush=8.0,
        expected_move=10.0, last_earnings_move=12.0, stock_price=price,
    )


def _scanner(events) -> EarningsScanner:
    return EarningsScanner(
        provider=_StubProvider(events),
        analyzer=IVHistoryAnalyzer(),
        builder=EarningsStrategyBuilder(),       # estimate path
        assessor=LLMEarningsAssessor(),          # rule-based (no key in tests)
    )


def test_scan_returns_opportunities_sorted_by_score() -> None:
    events = [_crush_event("NVDA"), _spike_event("TSLA"), _crush_event("AAPL", price=190.0)]
    opps = _scanner(events).scan(days_ahead=14)
    assert len(opps) == 3
    scores = [o.overall_score for o in opps]
    assert scores == sorted(scores, reverse=True)   # descending


def test_scan_classifies_crush_and_spike() -> None:
    opps = {o.ticker: o for o in _scanner([_crush_event(), _spike_event()]).scan()}
    assert opps["NVDA"].strategy == IV_CRUSH
    assert opps["TSLA"].strategy == IV_SPIKE
    assert opps["NVDA"].trade is not None
    assert opps["TSLA"].trade is not None


def test_scan_filters_penny_stocks() -> None:
    cheap = _crush_event("PENNY", price=5.0)   # below EARNINGS_MIN_STOCK_PRICE (20)
    opps = _scanner([cheap, _crush_event("NVDA")]).scan()
    assert [o.ticker for o in opps] == ["NVDA"]


def test_scan_filters_illiquid_options() -> None:
    illiquid = _crush_event("ILQ")
    illiquid.options_volume = 100              # below 1000
    opps = _scanner([illiquid, _crush_event("NVDA")]).scan()
    assert [o.ticker for o in opps] == ["NVDA"]


def test_priority_thresholds() -> None:
    assert EarningsScanner._priority(80, IV_CRUSH) == "HIGH"
    assert EarningsScanner._priority(60, IV_CRUSH) == "MEDIUM"
    assert EarningsScanner._priority(45, IV_CRUSH) == "LOW"
    assert EarningsScanner._priority(80, SKIP) == "SKIP"
    assert EarningsScanner._priority(0, IV_CRUSH) == "SKIP"


def test_score_zero_for_skip() -> None:
    assert EarningsScanner._score(IVAnalysis(ticker="X", strategy=SKIP), None, None) == 0


def test_scan_ticker_finds_single() -> None:
    scanner = _scanner([_crush_event("NVDA"), _spike_event("TSLA")])
    opp = scanner.scan_ticker("tsla")
    assert opp is not None
    assert opp.ticker == "TSLA"
    assert opp.strategy == IV_SPIKE


def test_scan_ticker_missing_returns_none() -> None:
    assert _scanner([_crush_event("NVDA")]).scan_ticker("ZZZZ") is None


def test_empty_scan() -> None:
    assert _scanner([]).scan() == []


def test_scan_uses_full_history_when_available() -> None:
    from broker_client.earnings.models import QuarterlyData

    class _HistProvider(_StubProvider):
        def get_iv_history(self, ticker):
            return EarningsIVHistory(
                ticker=ticker,
                quarters=[
                    QuarterlyData(iv_crush=25, expected_move=8, actual_move_close=3)
                    for _ in range(6)
                ],
            )

    scanner = EarningsScanner(
        provider=_HistProvider([_crush_event()]),
        analyzer=IVHistoryAnalyzer(),
        builder=EarningsStrategyBuilder(),
        assessor=LLMEarningsAssessor(),
    )
    opp = scanner.scan()[0]
    assert opp.iv_analysis.quarters_analyzed == 6   # full per-quarter path used


# ── CLI rendering ──────────────────────────────────────────────────────────────
def test_cli_render_lists_actionable() -> None:
    opps = _scanner([_crush_event(), _spike_event()]).scan()
    out = cli.render(opps, days=14)
    assert "EARNINGS OPPORTUNITIES" in out
    assert "NVDA" in out and "TSLA" in out
    assert "IV_CRUSH" in out and "IV_SPIKE" in out
    assert "EXIT" in out  # spike trade shows the hard exit


def test_cli_render_empty() -> None:
    out = cli.render([], days=7)
    assert "No actionable earnings plays" in out


def test_cli_main_runs(monkeypatch, capsys) -> None:
    # No broker, stub the scanner so main() runs end-to-end offline.
    scanner = _scanner([_crush_event()])
    monkeypatch.setattr(cli, "EarningsScanner", lambda **kw: scanner)
    monkeypatch.setattr(
        "broker_core.factory.get_broker", lambda: (_ for _ in ()).throw(RuntimeError("no broker"))
    )
    rc = cli.main(["--days", "14"])
    assert rc == 0
    assert "NVDA" in capsys.readouterr().out
