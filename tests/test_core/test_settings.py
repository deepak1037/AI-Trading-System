"""Tests for config.settings — pydantic-settings, fail-fast validation."""

from __future__ import annotations

import pytest

import config.settings as settings_module
from config.settings import Settings, settings
from core.exceptions import ConfigError


def test_module_singleton_loads() -> None:
    # Verify the singleton loaded without error and safety flags are consistent.
    assert settings.ENV in {"development", "backtest", "paper", "live"}
    assert isinstance(settings.DRY_RUN, bool)
    assert isinstance(settings.LIVE_TRADING_ENABLED, bool)


def test_defaults_match_spec() -> None:
    # Construct with explicit values so the real .env doesn't affect the test.
    s = Settings(ENV="development", BROKER="alpaca")
    assert s.BROKER == "alpaca"
    assert s.LOG_LEVEL == "INFO"
    assert s.MACRO_SURPRISE_CRITICAL == 2.0
    assert s.KELLY_FRACTION == 0.25
    assert s.POSITION_SCAN_INTERVAL_SECONDS == 60
    assert s.DB_PATH == "db/trading.db"


def test_collection_defaults_are_typed() -> None:
    s = Settings()
    assert s.ENABLED_STRATEGIES == ["equity_long_short"]
    assert s.REGIME_CLOSE_TRIGGERS == {
        "strong_short": ["equity_long_short", "momentum_breakout"]
    }


def test_fusion_weights_sum_to_100_by_default() -> None:
    s = Settings()
    total = (
        s.WEIGHT_MACRO
        + s.WEIGHT_YIELD
        + s.WEIGHT_SENTIMENT
        + s.WEIGHT_PREMARKET
        + s.WEIGHT_TECHNICAL
    )
    assert total == 100


def test_bad_weight_sum_raises() -> None:
    with pytest.raises(ConfigError, match="sum to 100"):
        Settings(WEIGHT_MACRO=99)


@pytest.mark.parametrize("bad_env", ["prod", "staging", "", "LIVE"])
def test_invalid_env_raises(bad_env: str) -> None:
    with pytest.raises(ConfigError, match="Invalid ENV"):
        Settings(ENV=bad_env)


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ConfigError, match="Invalid LOG_LEVEL"):
        Settings(LOG_LEVEL="verbose")


def test_invalid_broker_raises() -> None:
    with pytest.raises(ConfigError, match="Invalid BROKER"):
        Settings(BROKER="robinhood")


def test_invalid_fill_method_raises() -> None:
    with pytest.raises(ConfigError, match="Invalid PAPER_FILL_METHOD"):
        Settings(PAPER_FILL_METHOD="midpoint")


def test_live_gate_consistency_enforced() -> None:
    # LIVE_TRADING_ENABLED with DRY_RUN True is contradictory.
    with pytest.raises(ConfigError, match="incompatible with DRY_RUN"):
        Settings(LIVE_TRADING_ENABLED=True, DRY_RUN=True)


def test_env_live_requires_full_triple_gate() -> None:
    with pytest.raises(ConfigError, match="ENV=live requires"):
        Settings(ENV="live")  # DRY_RUN still True, LIVE_TRADING_ENABLED False


def test_valid_live_triple_gate_passes() -> None:
    s = Settings(ENV="live", DRY_RUN=False, LIVE_TRADING_ENABLED=True)
    assert s.ENV == "live"
    assert s.DRY_RUN is False
    assert s.LIVE_TRADING_ENABLED is True


def test_type_coercion_from_strings() -> None:
    # pydantic-settings coerces env-style strings to the declared types.
    s = Settings(DRY_RUN="False", CONFIDENCE_CRITICAL="90", DAILY_LOSS_LIMIT_PCT="0.03")
    assert s.DRY_RUN is False
    assert s.CONFIDENCE_CRITICAL == 90
    assert s.DAILY_LOSS_LIMIT_PCT == 0.03


def test_wrong_type_at_startup_raises_config_error(monkeypatch) -> None:
    # At the startup boundary, a wrong-type value is re-wrapped as ConfigError
    # (fail fast) rather than surfacing pydantic's ValidationError.
    monkeypatch.setenv("KELLY_FRACTION", "not_a_number")
    with pytest.raises(ConfigError, match="Failed to load settings"):
        settings_module._load_settings()


# ── Three-bucket framework (Phase 2) ─────────────────────────────────────────
def test_bucket_allocations_default_sum_to_100() -> None:
    s = Settings()
    total = (
        s.BUCKET1_ALLOCATION_PCT
        + s.BUCKET2_ALLOCATION_PCT
        + s.BUCKET3_ALLOCATION_PCT
    )
    assert total <= 100
    assert s.TRADING_PROFILE == "moderate"


def test_cash_buffer_pct_property() -> None:
    s = Settings(
        BUCKET1_ALLOCATION_PCT=60,
        BUCKET2_ALLOCATION_PCT=20,
        BUCKET3_ALLOCATION_PCT=15,
    )
    assert s.cash_buffer_pct == 5


def test_bucket_allocations_over_100_raises() -> None:
    with pytest.raises(ConfigError, match="sum to <= 100"):
        Settings(
            BUCKET1_ALLOCATION_PCT=70,
            BUCKET2_ALLOCATION_PCT=20,
            BUCKET3_ALLOCATION_PCT=20,
        )


def test_invalid_trading_profile_raises() -> None:
    with pytest.raises(ConfigError, match="Invalid TRADING_PROFILE"):
        Settings(TRADING_PROFILE="yolo")


def test_bucket_exit_thresholds_present() -> None:
    s = Settings()
    assert s.DTE_EXIT_THRESHOLD == 21
    assert s.BUCKET1_PROFIT_TARGET_PCT == 50.0
    assert s.BUCKET2B_STOP_LOSS_PCT == 200.0
    assert s.BUCKET3_LLM_REVIEW_PROFIT_PCT == 40.0
