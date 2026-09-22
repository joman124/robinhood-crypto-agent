"""Config can only make the agent more conservative."""

from decimal import Decimal

import pytest
import yaml

from robinhood_crypto_agent.config import (
    ABSOLUTE_CEILINGS,
    RiskLimits,
    StrategyConfig,
    load_config,
)
from robinhood_crypto_agent.errors import ConfigError
from robinhood_crypto_agent.models import ExecutionMode


def write(tmp_path, **files):
    for name, payload in files.items():
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(payload))
    return tmp_path


@pytest.mark.parametrize("name,ceiling", sorted(ABSOLUTE_CEILINGS.items()))
def test_no_limit_may_exceed_its_code_ceiling(name, ceiling):
    with pytest.raises(ConfigError, match="hard ceiling"):
        RiskLimits.from_mapping({name: ceiling + Decimal("1")})


def test_a_limit_at_the_ceiling_is_allowed():
    assert RiskLimits.from_mapping(
        {"max_spread_pct": ABSOLUTE_CEILINGS["max_spread_pct"]}
    ).max_spread_pct == ABSOLUTE_CEILINGS["max_spread_pct"]


def test_tightening_a_limit_is_always_allowed():
    limits = RiskLimits.from_mapping({"max_notional_per_trade_usd": 25})
    assert limits.max_notional_per_trade_usd == Decimal("25")


def test_floors_are_enforced_too():
    with pytest.raises(ConfigError, match="hard floor"):
        RiskLimits.from_mapping({"min_signal_confidence": 0.01})


def test_non_positive_limits_are_rejected():
    with pytest.raises(ConfigError, match="must be positive"):
        RiskLimits.from_mapping({"max_daily_loss_usd": 0})


def test_inconsistent_limits_are_rejected():
    with pytest.raises(ConfigError, match="could not pass the daily cap"):
        RiskLimits.from_mapping(
            {"max_notional_per_trade_usd": 500, "max_daily_notional_usd": 100}
        )
    with pytest.raises(ConfigError, match="exceeds"):
        RiskLimits.from_mapping(
            {"min_notional_per_trade_usd": 300, "max_notional_per_trade_usd": 200}
        )


def test_unknown_limits_are_rejected_not_ignored():
    """A typo must fail loudly, not silently leave the default in place."""
    with pytest.raises(ConfigError, match="unknown risk limit"):
        RiskLimits.from_mapping({"max_notional_per_trade": 10})


def test_unknown_strategy_settings_are_rejected():
    with pytest.raises(ConfigError, match="unknown strategy setting"):
        StrategyConfig.from_mapping({"rsi_perid": 14})


def test_fast_ma_must_be_shorter_than_slow():
    with pytest.raises(ConfigError, match="must be shorter"):
        StrategyConfig.from_mapping({"fast_ma": 30, "slow_ma": 20})


def test_weights_are_normalized_to_sum_to_one():
    config = StrategyConfig.from_mapping(
        {
            "weights": {
                "trending": {"trend": 3, "momentum": 1, "breakout": 0, "mean_reversion": 0}
            }
        }
    )
    weights = config.weights_for("trending")
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["trend"] == pytest.approx(0.75)


def test_incomplete_weights_are_rejected():
    with pytest.raises(ConfigError, match="is missing"):
        StrategyConfig.from_mapping({"weights": {"trending": {"trend": 1}}})


def test_negative_weights_are_rejected():
    with pytest.raises(ConfigError, match="must not be negative"):
        StrategyConfig.from_mapping(
            {"weights": {"trending": {"trend": -1, "momentum": 1, "breakout": 1, "mean_reversion": 1}}}
        )


def test_shipped_config_loads_and_is_within_ceilings():
    """The configuration committed to this repo must actually be valid."""
    config = load_config("config")
    assert config.execution_mode is ExecutionMode.PROPOSE_ONLY
    assert config.watchlist
    config.risk.validate()
    config.strategy.validate()


def test_missing_config_directory_falls_back_to_defaults(tmp_path):
    config = load_config(tmp_path / "absent")
    assert config.execution_mode is ExecutionMode.PROPOSE_ONLY
    assert config.watchlist == ("BTC-USD", "ETH-USD")


def test_present_but_invalid_config_is_an_error_not_a_fallback(tmp_path):
    (tmp_path / "risk_limits.yaml").write_text("limits: {max_daily_loss_usd: 999999}")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_watchlist_is_canonicalized_and_deduplicated(tmp_path):
    write(tmp_path, agent={"watchlist": ["BTCUSD", "btc-usd", "ETH/USD"]})
    assert load_config(tmp_path).watchlist == ("BTC-USD", "ETH-USD")


def test_empty_watchlist_is_rejected(tmp_path):
    write(tmp_path, agent={"watchlist": []})
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(tmp_path)


def test_unknown_execution_mode_is_rejected(tmp_path):
    write(tmp_path, agent={"execution_mode": "yolo"})
    with pytest.raises(ConfigError, match="execution_mode must be"):
        load_config(tmp_path)


def test_account_number_comes_from_the_environment_first(tmp_path, monkeypatch):
    """So a real account number never has to be committed."""
    write(tmp_path, agent={"rhs_account_number": "111"})
    monkeypatch.setenv("RHCA_RHS_ACCOUNT_NUMBER", "999")
    assert load_config(tmp_path).rhs_account_number == "999"


def test_allows_matches_across_symbol_spellings(tmp_path):
    write(tmp_path, agent={"watchlist": ["BTC-USD"]})
    config = load_config(tmp_path)
    assert config.allows("BTCUSD")
    assert not config.allows("DOGE-USD")


class TestPipelineConfig:
    """The pipeline's knobs bound spend and politeness, so they get ceilings too."""

    def test_the_shipped_pipeline_file_loads(self):
        pipeline = load_config("config").pipeline
        assert pipeline.system2_model == "claude-sonnet-5"
        assert pipeline.rss_feeds and all(u.startswith("https://") for u in pipeline.rss_feeds)

    @pytest.mark.parametrize(
        "setting",
        [
            {"max_escalations_per_day": 201},
            {"quote_interval_seconds": 1},
            {"trigger_min_confidence": 1.5},
            {"rss_feeds": ["http://insecure.example/rss"]},
            {"market_data_mcp_url": "http://insecure.example/mcp"},
            {"no_such_setting": 1},
        ],
    )
    def test_out_of_bounds_settings_are_refused(self, tmp_path, setting):
        write(tmp_path, pipeline={"pipeline": setting})
        with pytest.raises(ConfigError):
            load_config(tmp_path)
