from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from robinhood_crypto_agent.risk.limits import RiskLimits

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ExecutionMode(str, Enum):
    PROPOSE_ONLY = "propose_only"
    AUTO = "auto"


class RegimeConfig(BaseModel):
    adx_period: int = 14
    adx_trending_threshold: float = 25.0


class StrategyWeights(BaseModel):
    regimes: dict[str, dict[str, float]]
    signal_threshold: float
    min_confidence: float
    regime: RegimeConfig = Field(default_factory=RegimeConfig)


class AppConfig(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.PROPOSE_ONLY
    risk_limits: RiskLimits
    watchlist: list[str]
    strategy_weights: StrategyWeights


def load_config(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    execution_mode: ExecutionMode = ExecutionMode.PROPOSE_ONLY,
) -> AppConfig:
    """Load watchlist, risk limits, and strategy weights from config/*.yaml.

    `execution_mode` is passed explicitly rather than read from YAML - see
    CLAUDE.md rule 6: switching to AUTO is a deliberate, separately-confirmed
    human action, never something a config file alone can silently do.
    """
    watchlist_data = yaml.safe_load((config_dir / "watchlist.yaml").read_text())
    risk_limits_data = yaml.safe_load((config_dir / "risk_limits.yaml").read_text())
    strategy_weights_data = yaml.safe_load(
        (config_dir / "strategy_weights.yaml").read_text()
    )

    symbols: list[str] = watchlist_data["symbols"]
    risk_limits = RiskLimits(allowed_symbols=symbols, **risk_limits_data)
    strategy_weights = StrategyWeights(**strategy_weights_data)

    return AppConfig(
        execution_mode=execution_mode,
        risk_limits=risk_limits,
        watchlist=symbols,
        strategy_weights=strategy_weights,
    )
