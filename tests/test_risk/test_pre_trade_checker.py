"""Tests for broker_client/risk/pre_trade_checker.py."""

from __future__ import annotations

import pytest

from broker_client.risk.pre_trade_checker import PreTradeChecker, PreTradeResult, ProposedTrade


class _FakePaper:
    def __init__(self, cash: float = 50_000, equity: float = 100_000):
        self._cash = cash
        self._equity = equity

    class _State:
        pass

    def get_state(self):
        s = self._State()
        s.cash = self._cash
        s.equity = self._equity
        return s


# ── cash buffer ────────────────────────────────────────────────────────────────

def test_blocks_when_cash_insufficient(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 10.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 20.0)

    paper = _FakePaper(cash=5_000, equity=100_000)
    checker = PreTradeChecker(paper_account=paper)
    trade = ProposedTrade(ticker="NVDA", bucket=1, margin_estimated=8_000)
    result = checker.check(trade, [], portfolio_value=100_000)

    assert result.approved is False
    assert result.recommendation == "BLOCK"
    assert any("cash" in b.lower() for b in result.hard_blocks)


def test_warns_when_cash_low_but_not_critical(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 5.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 20.0)

    paper = _FakePaper(cash=15_000, equity=100_000)
    checker = PreTradeChecker(paper_account=paper)
    trade = ProposedTrade(ticker="JNJ", bucket=1, margin_estimated=3_000)
    result = checker.check(trade, [], portfolio_value=100_000)
    assert any("low" in w.lower() or "cash" in w.lower() for w in result.warnings)


# ── bucket capacity ────────────────────────────────────────────────────────────

def test_blocks_when_bucket_capacity_exceeded(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "BUCKET1_ALLOCATION_PCT", 10)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)

    paper = _FakePaper(cash=90_000, equity=100_000)

    class _MockBucketManager:
        def get_bucket_positions(self, b): return []

    from broker_client.risk.pre_trade_checker import PreTradeChecker as _PC
    checker = _PC(paper_account=paper)
    checker._buckets = _MockBucketManager()  # type: ignore[attr-defined]

    # Bucket 1 max = 10% × 100k = 10k; trade wants 20k → should block
    trade = ProposedTrade(ticker="JNJ", bucket=1, margin_estimated=20_000)
    result = checker.check(trade, [], portfolio_value=100_000)
    assert result.approved is False
    assert any("capacity" in b.lower() or "bucket" in b.lower() for b in result.hard_blocks)


# ── sector ─────────────────────────────────────────────────────────────────────

def test_blocks_when_sector_critical(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 30.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 50.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)
    monkeypatch.setattr(settings, "BUCKET1_ALLOCATION_PCT", 100)

    paper = _FakePaper(cash=80_000, equity=100_000)

    class _MockBM:
        def get_bucket_positions(self, b): return []

    checker = PreTradeChecker(paper_account=paper)
    checker._buckets = _MockBM()  # type: ignore[attr-defined]

    # Large AMD (Semiconductors) position + add more AMD → sector > 50%
    existing = [
        {"ticker": "AMD", "option_type": "put", "position_type": "options_short",
         "qty": 1, "entry_price": 300.0},  # 300 × 100 = 30k
    ]
    trade = ProposedTrade(ticker="AMD", bucket=1, margin_estimated=30_000)
    result = checker.check(trade, existing, portfolio_value=100_000)
    assert result.recommendation in ("BLOCK", "REVIEW")


# ── earnings ───────────────────────────────────────────────────────────────────

def test_warns_when_earnings_overlap(monkeypatch, tmp_path):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 100.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 100.0)
    monkeypatch.setattr(settings, "BUCKET1_ALLOCATION_PCT", 100)

    from datetime import date, timedelta
    from data.db import get_connection, init_db

    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    earn_date = (date.today() + timedelta(days=7)).isoformat()

    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO earnings_opportunities (ticker, earnings_date, strategy, overall_score) "
            "VALUES (?, ?, 'IV_CRUSH', 70)",
            ("AAPL", earn_date),
        )

    paper = _FakePaper(cash=80_000, equity=100_000)

    class _MockBM:
        def get_bucket_positions(self, b): return []

    checker = PreTradeChecker(paper_account=paper, db_path=db_path)
    checker._buckets = _MockBM()  # type: ignore[attr-defined]

    trade = ProposedTrade(ticker="AAPL", bucket=1, margin_estimated=5_000,
                          expiry=(date.today() + timedelta(days=30)).isoformat())
    result = checker.check(trade, [], portfolio_value=100_000)
    assert any("earnings" in w.lower() for w in result.warnings)


# ── watchlist ──────────────────────────────────────────────────────────────────

def test_warns_when_not_on_watchlist(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 100.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 100.0)
    monkeypatch.setattr(settings, "BUCKET1_ALLOCATION_PCT", 100)

    paper = _FakePaper(cash=80_000, equity=100_000)

    class _MockBM:
        def get_bucket_positions(self, b): return []

    checker = PreTradeChecker(paper_account=paper)
    checker._buckets = _MockBM()  # type: ignore[attr-defined]

    trade = ProposedTrade(ticker="XYZ", bucket=1, margin_estimated=5_000,
                          is_on_watchlist=False)
    result = checker.check(trade, [], portfolio_value=100_000)
    assert any("watchlist" in w.lower() for w in result.warnings)


# ── Bucket 3 size ──────────────────────────────────────────────────────────────

def test_bucket3_size_limit(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "BUCKET3_MAX_SINGLE_TRADE_PCT", 2.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 0.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 0.0)
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 100.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 100.0)
    monkeypatch.setattr(settings, "BUCKET3_ALLOCATION_PCT", 10)

    paper = _FakePaper(cash=80_000, equity=100_000)

    class _MockBM:
        def get_bucket_positions(self, b): return []

    checker = PreTradeChecker(paper_account=paper)
    checker._buckets = _MockBM()  # type: ignore[attr-defined]

    # Max = 2% × 100k = 2k; paying 5k premium → should block
    trade = ProposedTrade(ticker="NVDA", bucket=3, premium_paid=5_000,
                          margin_estimated=5_000)
    result = checker.check(trade, [], portfolio_value=100_000)
    assert result.approved is False
    assert any("bucket 3" in b.lower() for b in result.hard_blocks)


# ── happy path ────────────────────────────────────────────────────────────────

def test_approves_healthy_trade(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CASH_BUFFER_CRITICAL_PCT", 5.0)
    monkeypatch.setattr(settings, "CASH_BUFFER_WARNING_PCT", 10.0)
    # 3k/100k = 3% sector → well below 40% warning
    monkeypatch.setattr(settings, "SECTOR_WARNING_PCT", 40.0)
    monkeypatch.setattr(settings, "SECTOR_CRITICAL_PCT", 60.0)
    monkeypatch.setattr(settings, "SINGLE_TICKER_WARNING_PCT", 30.0)
    monkeypatch.setattr(settings, "SAME_EXPIRY_WARNING", 5)
    monkeypatch.setattr(settings, "BUCKET1_ALLOCATION_PCT", 70)
    monkeypatch.setattr(settings, "BUCKET3_MAX_SINGLE_TRADE_PCT", 2.0)

    paper = _FakePaper(cash=60_000, equity=100_000)

    class _MockBM:
        def get_bucket_positions(self, b): return []

    checker = PreTradeChecker(paper_account=paper)
    checker._buckets = _MockBM()  # type: ignore[attr-defined]

    # JNJ Healthcare: 3k on 100k portfolio = 3% sector concentration
    trade = ProposedTrade(ticker="JNJ", bucket=1, margin_estimated=3_000)
    result = checker.check(trade, [], portfolio_value=100_000)
    assert result.approved is True
    assert result.recommendation == "PROCEED"
