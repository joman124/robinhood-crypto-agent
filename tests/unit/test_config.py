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
    with pytest.raises(ConfigError, match="floor"):
        RiskLimits.from_mapping({"min_notional_per_trade_usd": 0.5})


def test_system_1s_limits_are_gone_not_ignored():
    """A config still naming them fails loudly rather than silently dropping them."""
    for name in ("min_signal_confidence", "min_abs_score", "disable_sell_side"):
        with pytest.raises(ConfigError, match="unknown risk limit"):
            RiskLimits.from_mapping({name: 1})


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


class TestStrategyConfig:
    def test_the_defaults_are_the_backtested_rule(self):
        config = StrategyConfig()
        ladder = config.ladder()
        assert ladder.mode == "anchor" and ladder.trend_filter and ladder.trend_exit
        assert ladder.steps == ((5, 5), (10, 10), (20, 20))
        assert config.trend_bars == 1200 and config.required_bars == 1200
        assert config.history_days == 52

    def test_steps_read_as_a_string_or_a_list(self):
        assert StrategyConfig.from_mapping({"steps": "10:10,5:5"}).steps == ((5, 5), (10, 10))
        assert StrategyConfig.from_mapping({"steps": ["5:5", "10:20"]}).steps == (
            (5, 5),
            (10, 20),
        )
        with pytest.raises(ConfigError, match="steps"):
            StrategyConfig.from_mapping({"steps": "5:0"})

    def test_no_trend_window_means_no_filter_and_no_exit(self):
        config = StrategyConfig.from_mapping({"trend_days": 0})
        assert not config.ladder().trend_filter and not config.ladder().trend_exit
        assert config.required_bars == 1

    def test_trend_exit_must_be_a_boolean(self):
        assert StrategyConfig.from_mapping({"trend_exit": False}).ladder().trend_exit is False
        with pytest.raises(ConfigError):
            StrategyConfig.from_mapping({"trend_exit": "no"})

    def test_anchor_since_reads_a_date(self, tmp_path):
        write(tmp_path, strategy={"strategy": {"anchor_since": "2026-09-27"}})
        since = load_config(tmp_path).strategy.anchor_since
        assert since.isoformat() == "2026-09-27T00:00:00+00:00"

    def test_negative_days_are_refused(self):
        with pytest.raises(ConfigError):
            StrategyConfig.from_mapping({"trend_days": -1})


def test_shipped_config_loads_and_is_within_ceilings():
    """The configuration committed to this repo must actually be valid."""
    config = load_config("config")
    assert config.execution_mode is ExecutionMode.PROPOSE_ONLY
    assert config.watchlist == ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")
    assert config.risk.max_position_pct_of_portfolio == 20  # the owner's, 2026-09-28
    config.risk.validate()
    config.strategy.validate()
    assert config.strategy.trend_days == 50 and config.strategy.trend_exit


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
    """The pipeline's cadences have floors, so a typo cannot hammer an API."""

    def test_the_shipped_pipeline_file_loads(self):
        pipeline = load_config("config").pipeline
        assert pipeline.quote_interval_seconds == 60

    @pytest.mark.parametrize(
        "setting",
        [
            {"quote_interval_seconds": 1},
            {"sync_interval_seconds": 5},
            {"max_escalations_per_day": 24},  # System 2's, retired
            {"no_such_setting": 1},
        ],
    )
    def test_out_of_bounds_settings_are_refused(self, tmp_path, setting):
        write(tmp_path, pipeline={"pipeline": setting})
        with pytest.raises(ConfigError):
            load_config(tmp_path)
