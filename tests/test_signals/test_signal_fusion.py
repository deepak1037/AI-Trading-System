"""Tests for signals/signal_fusion.py and watcher/market_state.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


from signals.signal_fusion import SignalFusion, _score_to_regime
from signals.signal_schema import Signal
from watcher.market_state import MarketStateTracker


def _sig(direction, source="macro", confidence=70) -> Signal:
    return Signal(
        direction=direction,
        confidence=confidence,
        source=source,
        timestamp=datetime.now(tz=timezone.utc),
    )


class TestScoreToRegime:
    def test_strong_long(self):
        assert _score_to_regime(80) == "strong_long"

    def test_long(self):
        assert _score_to_regime(60) == "long"

    def test_neutral(self):
        assert _score_to_regime(50) == "neutral"

    def test_short(self):
        assert _score_to_regime(40) == "short"

    def test_strong_short(self):
        assert _score_to_regime(20) == "strong_short"


class TestSignalFusion:
    def test_all_bullish_signals_produce_long(self):
        fusion = SignalFusion()
        signals = [
            _sig("long", "macro", 80),
            _sig("long", "yield", 70),
            _sig("long", "sentiment", 65),
            _sig("long", "premarket", 75),
            _sig("long", "technical", 60),
        ]
        state = fusion.fuse(signals)
        assert state.current_regime in ("long", "strong_long")

    def test_all_bearish_signals_produce_short(self):
        fusion = SignalFusion()
        signals = [
            _sig("short", "macro", 80),
            _sig("short", "yield", 70),
            _sig("short", "sentiment", 65),
            _sig("short", "premarket", 75),
            _sig("short", "technical", 60),
        ]
        state = fusion.fuse(signals)
        assert state.current_regime in ("short", "strong_short")

    def test_mixed_signals_near_neutral(self):
        fusion = SignalFusion()
        signals = [
            _sig("long", "macro", 60),
            _sig("short", "yield", 60),
            _sig("neutral", "sentiment", 50),
        ]
        state = fusion.fuse(signals)
        assert state.current_regime in ("neutral", "long", "short")
        assert 0 <= state.composite_score <= 100

    def test_regime_change_detected(self):
        fusion = SignalFusion()
        # First: bullish
        state1 = fusion.fuse([_sig("long", "macro", 80), _sig("long", "yield", 80)])
        # Then: bearish
        state2 = fusion.fuse([_sig("short", "macro", 90), _sig("short", "yield", 90)])
        assert state2.regime_changed is True
        assert state2.previous_regime == state1.current_regime

    def test_no_change_on_same_regime(self):
        fusion = SignalFusion()
        fusion.fuse([_sig("neutral", "macro", 50), _sig("neutral", "yield", 50)])
        state2 = fusion.fuse([_sig("neutral", "macro", 52), _sig("neutral", "yield", 48)])
        assert state2.regime_changed is False

    def test_tf_alignment_bonus_applied(self):
        fusion = SignalFusion()
        # All bullish — bonus should push composite higher than without bonus
        signals = [
            _sig("long", "macro", 60),
            _sig("long", "yield", 60),
            _sig("long", "sentiment", 60),
            _sig("long", "premarket", 60),
            _sig("long", "technical", 60),
        ]
        state = fusion.fuse(signals)
        # With TF bonus, composite should be higher
        assert state.composite_score > 50

    def test_empty_signals_returns_neutral(self):
        fusion = SignalFusion()
        state = fusion.fuse([])
        assert state.current_regime == "neutral"
        assert state.composite_score == 50

    def test_fusion_signal_source_ignored(self):
        fusion = SignalFusion()
        signals = [_sig("long", "fusion", 90)]  # fusion source has weight=0
        state = fusion.fuse(signals)
        assert state.composite_score == 50  # no weighted input


class TestMarketStateTracker:
    def test_transition_callback_fires_on_change(self):
        callback = MagicMock()
        tracker = MarketStateTracker(on_transition=callback)
        tracker.update([_sig("long", "macro", 80), _sig("long", "yield", 80)])
        # Initial state is a transition (from None)
        callback.assert_called_once()

    def test_transition_callback_not_fired_on_no_change(self):
        callback = MagicMock()
        tracker = MarketStateTracker(on_transition=callback)
        tracker.update([_sig("neutral", "macro", 50)])
        callback.reset_mock()
        tracker.update([_sig("neutral", "macro", 52)])
        callback.assert_not_called()

    def test_callback_exception_does_not_propagate(self):
        def bad_cb(state):
            raise RuntimeError("boom")

        tracker = MarketStateTracker(on_transition=bad_cb)
        # Should not raise
        state = tracker.update([_sig("long", "macro", 80)])
        assert state is not None


class TestComputeAndCollect:
    def test_compute_fuses_collected_signals(self, monkeypatch):
        f = SignalFusion()
        monkeypatch.setattr(f, "collect_signals", lambda benchmark="SPY", phase=None: [
            _sig("long", source="technical", confidence=60),
        ])
        state = f.compute()
        assert state.current_regime in ("strong_short", "short", "neutral", "long", "strong_long")
        assert 0 <= state.composite_score <= 100
        assert state.confidence == 60  # mean of one signal

    def test_compute_empty_signals_is_neutral(self, monkeypatch):
        f = SignalFusion()
        monkeypatch.setattr(f, "collect_signals", lambda benchmark="SPY", phase=None: [])
        state = f.compute()
        assert state.current_regime == "neutral"
        assert state.composite_score == 50
        assert state.confidence == 0

    def test_collect_signals_guards_source_failures(self, monkeypatch):
        # Make the technical source blow up; collect_signals must not raise.
        import signals.technical_module as tm

        class _Boom:
            def score_ticker(self, *a, **k):
                raise RuntimeError("yfinance down")

        monkeypatch.setattr(tm, "TechnicalModule", _Boom)
        f = SignalFusion()
        # yield source also unconfigured in test env → both guarded → []
        result = f.collect_signals()
        assert isinstance(result, list)


class TestMarketStateProperties:
    def _state(self, regime, sigs):
        from signals.signal_schema import MarketState
        return MarketState(
            current_regime=regime, composite_score=50,
            last_updated=datetime.now(tz=timezone.utc), signals_active=sigs,
        )

    def test_direction_aliases_regime(self):
        st = self._state("long", [])
        assert st.direction == "long" == st.current_regime

    def test_confidence_is_mean(self):
        st = self._state("neutral", [_sig("neutral", confidence=30), _sig("long", confidence=50)])
        assert st.confidence == 40

    def test_confidence_zero_when_no_signals(self):
        assert self._state("neutral", []).confidence == 0


class TestSentimentAndPremarketGating:
    def _mock_core_sources(self, monkeypatch):
        # Replace network sources so collect_signals is hermetic.
        class _Tech:
            def score_ticker(self, *a, **k):
                return _sig("neutral", source="technical", confidence=34)

        class _Yield:
            def check_delta(self):
                return None  # FRED not configured

        monkeypatch.setattr("signals.technical_module.TechnicalModule", _Tech)
        monkeypatch.setattr("signals.yield_monitor.YieldMonitor", _Yield)

    def test_sentiment_skipped_without_news_key(self, monkeypatch):
        monkeypatch.setattr("signals.signal_fusion.settings.NEWS_API_KEY", "")
        f = SignalFusion()
        assert f._ensure_sentiment() is None

    def test_sentiment_loaded_once_with_news_key(self, monkeypatch):
        monkeypatch.setattr("signals.signal_fusion.settings.NEWS_API_KEY", "k")
        loaded = {"n": 0}

        class _Scorer:
            def ensure_loaded(self):
                loaded["n"] += 1

            def score(self):
                return _sig("long", source="sentiment", confidence=60)

        monkeypatch.setattr("signals.sentiment_scorer.SentimentScorer", _Scorer)
        f = SignalFusion()
        assert f._ensure_sentiment() is not None
        f._ensure_sentiment()  # cached — must NOT reload
        assert loaded["n"] == 1

    def test_sentiment_included_when_configured(self, monkeypatch):
        self._mock_core_sources(monkeypatch)
        monkeypatch.setattr("signals.signal_fusion.settings.NEWS_API_KEY", "k")

        class _Scorer:
            def ensure_loaded(self):
                pass

            def score(self):
                return _sig("long", source="sentiment", confidence=60)

        monkeypatch.setattr("signals.sentiment_scorer.SentimentScorer", _Scorer)
        sources = [s.source for s in SignalFusion().collect_signals(phase="session")]
        assert "sentiment" in sources

    def test_premarket_polled_in_preopen_phase(self, monkeypatch):
        self._mock_core_sources(monkeypatch)
        monkeypatch.setattr("signals.signal_fusion.settings.NEWS_API_KEY", "")

        class _PW:
            def check(self):
                return _sig("short", source="premarket", confidence=50)

        monkeypatch.setattr("signals.premarket_watcher.PremarketWatcher", _PW)
        for phase in ("premarket", "macro"):
            sources = [s.source for s in SignalFusion().collect_signals(phase=phase)]
            assert "premarket" in sources, phase

    def test_premarket_skipped_during_session(self, monkeypatch):
        self._mock_core_sources(monkeypatch)
        monkeypatch.setattr("signals.signal_fusion.settings.NEWS_API_KEY", "")

        class _PW:
            def check(self):
                return _sig("short", source="premarket", confidence=50)

        monkeypatch.setattr("signals.premarket_watcher.PremarketWatcher", _PW)
        sources = [s.source for s in SignalFusion().collect_signals(phase="session")]
        assert "premarket" not in sources
