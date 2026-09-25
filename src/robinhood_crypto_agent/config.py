"""Configuration loading, bounded by ceilings that config cannot raise.

The design rule here: **config can only ever make the agent more conservative.**
Every numeric risk limit is clamped against a ceiling defined in this file, in
code. If ``config/risk_limits.yaml`` asks for a per-trade cap of $1,000,000, the
load fails loudly rather than granting it. That way a risk control cannot be
loosened by editing a YAML file -- widening a limit is a code change, which is
reviewable and shows up in a diff.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .models import ExecutionMode
from .numeric import ZERO, to_decimal
from .symbols import canonical

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_DATA_DIR = Path("data")

#: Hard upper bounds. Config may go lower, never higher.
#:
#: These are not "sensible defaults" -- they are the outer edge of what this
#: codebase will ever emit a proposal for, regardless of configuration.
ABSOLUTE_CEILINGS: dict[str, Decimal] = {
    "max_notional_per_trade_usd": Decimal("2500"),
    "max_daily_notional_usd": Decimal("10000"),
    "max_daily_loss_usd": Decimal("1000"),
    "max_position_pct_of_portfolio": Decimal("25"),
    # Raised from 8 at the owner's instruction, 2026-09-22.
    "max_open_positions": Decimal("15"),
    "max_spread_pct": Decimal("2.5"),
    "price_drift_tolerance_pct": Decimal("1.5"),
    "max_quote_age_seconds": Decimal("300"),
}

#: Floors, for limits where "too small" is the unsafe direction.
ABSOLUTE_FLOORS: dict[str, Decimal] = {
    "min_notional_per_trade_usd": Decimal("1"),
    "min_signal_confidence": Decimal("0.15"),
}


@dataclass(frozen=True)
class RiskLimits:
    """Per-trade and per-day trading limits, already clamped to the ceilings."""

    max_notional_per_trade_usd: Decimal = Decimal("250")
    min_notional_per_trade_usd: Decimal = Decimal("10")
    max_daily_notional_usd: Decimal = Decimal("1000")
    max_daily_loss_usd: Decimal = Decimal("200")
    max_position_pct_of_portfolio: Decimal = Decimal("10")
    max_open_positions: int = 5
    max_spread_pct: Decimal = Decimal("0.75")
    price_drift_tolerance_pct: Decimal = Decimal("0.5")
    max_quote_age_seconds: int = 90
    min_signal_confidence: Decimal = Decimal("0.35")
    min_abs_score: Decimal = Decimal("0.25")
    #: Every sell proposal that has resolved so far has lost or gone flat (0/52
    #: decided as of 2026-09-25). Config can only make the agent more
    #: conservative, so this is a boolean off-switch rather than a threshold --
    #: there is no "less strict" version of a rule with a unanimous result.
    disable_sell_side: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "RiskLimits":
        data = dict(data or {})
        limits = cls()
        for name in (
            "max_notional_per_trade_usd",
            "min_notional_per_trade_usd",
            "max_daily_notional_usd",
            "max_daily_loss_usd",
            "max_position_pct_of_portfolio",
            "max_spread_pct",
            "price_drift_tolerance_pct",
            "min_signal_confidence",
            "min_abs_score",
        ):
            if name in data and data[name] is not None:
                limits = replace(limits, **{name: to_decimal(data[name], field=name)})
        for name in ("max_open_positions", "max_quote_age_seconds"):
            if name in data and data[name] is not None:
                limits = replace(limits, **{name: int(data[name])})
        if "disable_sell_side" in data and data["disable_sell_side"] is not None:
            limits = replace(limits, disable_sell_side=bool(data["disable_sell_side"]))

        unknown = set(data) - set(vars(limits))
        if unknown:
            raise ConfigError(f"unknown risk limit(s): {', '.join(sorted(unknown))}")
        limits.validate()
        return limits

    def validate(self) -> None:
        """Enforce the code ceilings and internal consistency."""
        for name, ceiling in ABSOLUTE_CEILINGS.items():
            value = to_decimal(getattr(self, name), field=name)
            if value > ceiling:
                raise ConfigError(
                    f"{name}={value} exceeds the hard ceiling of {ceiling} set in config.py. "
                    "Raising a risk ceiling is a deliberate code change, not a config edit."
                )
            if value <= ZERO:
                raise ConfigError(f"{name} must be positive, got {value}")

        for name, floor in ABSOLUTE_FLOORS.items():
            value = to_decimal(getattr(self, name), field=name)
            if value < floor:
                raise ConfigError(f"{name}={value} is below the hard floor of {floor}")

        if self.min_notional_per_trade_usd > self.max_notional_per_trade_usd:
            raise ConfigError(
                f"min_notional_per_trade_usd={self.min_notional_per_trade_usd} exceeds "
                f"max_notional_per_trade_usd={self.max_notional_per_trade_usd}"
            )
        if self.max_notional_per_trade_usd > self.max_daily_notional_usd:
            raise ConfigError(
                f"max_notional_per_trade_usd={self.max_notional_per_trade_usd} exceeds "
                f"max_daily_notional_usd={self.max_daily_notional_usd}: a single trade "
                "could not pass the daily cap"
            )
        if not ZERO < self.min_abs_score <= Decimal(1):
            raise ConfigError(f"min_abs_score must be in (0, 1], got {self.min_abs_score}")


@dataclass(frozen=True)
class StrategyConfig:
    """Indicator parameters, regime weights, and the bar interval."""

    bar_interval_minutes: int = 60
    min_bars: int = 30
    fast_ma: int = 12
    slow_ma: int = 26
    signal_ma: int = 9
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    adx_trend_threshold: Decimal = Decimal("22")
    breakout_lookback: int = 20
    #: Per-bar volatility the sizing model targets, in percent. Size is cut
    #: proportionally when realized volatility runs above this.
    target_volatility_pct: Decimal = Decimal("1.0")
    volatility_lookback: int = 20
    #: How long a headline can move the news signal; its confidence decays
    #: linearly to zero across this window.
    news_window_minutes: int = 120
    #: ``news`` is optional: it only counts when a recent headline exists (see
    #: strategy.composite), so its weight never dilutes a quiet-news blend.
    weights: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "trending": {
                "trend": 0.50,
                "momentum": 0.25,
                "breakout": 0.15,
                "mean_reversion": 0.10,
                "news": 0.50,
            },
            "ranging": {
                "mean_reversion": 0.50,
                "trend": 0.15,
                "momentum": 0.15,
                "breakout": 0.20,
                "news": 0.50,
            },
            "unknown": {
                "trend": 0.25,
                "momentum": 0.25,
                "breakout": 0.25,
                "mean_reversion": 0.25,
                "news": 0.50,
            },
        }
    )

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "StrategyConfig":
        data = dict(data or {})
        weights = data.pop("weights", None)
        config = cls()
        for name, value in data.items():
            if not hasattr(config, name):
                raise ConfigError(f"unknown strategy setting: {name}")
            if value is None:
                continue
            current = getattr(config, name)
            coerced = to_decimal(value, field=name) if isinstance(current, Decimal) else int(value)
            config = replace(config, **{name: coerced})
        if weights is not None:
            # Merge over the defaults rather than replacing them: configuring
            # the trending weights must not delete the ranging and unknown
            # ones, which would leave weights_for() with no entry to fall back
            # to for an unconfigured regime.
            merged = {regime: dict(w) for regime, w in cls().weights.items()}
            merged.update(_normalize_weights(weights))
            config = replace(config, weights=merged)
        config.validate()
        return config

    def validate(self) -> None:
        if self.fast_ma >= self.slow_ma:
            raise ConfigError(
                f"fast_ma={self.fast_ma} must be shorter than slow_ma={self.slow_ma}"
            )
        if self.target_volatility_pct <= ZERO:
            raise ConfigError("target_volatility_pct must be positive")
        for name in ("bar_interval_minutes", "min_bars", "rsi_period", "atr_period",
                     "adx_period", "breakout_lookback", "signal_ma", "volatility_lookback",
                     "news_window_minutes"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        required = {"trend", "momentum", "breakout", "mean_reversion"}
        for regime, weights in self.weights.items():
            missing = required - set(weights)
            if missing:
                raise ConfigError(f"weights[{regime}] is missing: {', '.join(sorted(missing))}")

    def weights_for(self, regime: str) -> dict[str, float]:
        """Weights for a regime, falling back to the equal-weight 'unknown' set."""
        return dict(self.weights.get(regime) or self.weights.get("unknown") or {})


def _normalize_weights(raw: Any) -> dict[str, dict[str, float]]:
    """Validate and L1-normalize each regime's weights so they sum to 1."""
    if not isinstance(raw, dict):
        raise ConfigError("strategy weights must be a mapping of regime -> weights")
    normalized: dict[str, dict[str, float]] = {}
    for regime, weights in raw.items():
        if not isinstance(weights, dict) or not weights:
            raise ConfigError(f"weights[{regime}] must be a non-empty mapping")
        values = {}
        for name, weight in weights.items():
            value = float(weight)
            if value < 0:
                raise ConfigError(f"weights[{regime}][{name}]={value} must not be negative")
            values[str(name)] = value
        total = sum(values.values())
        if total <= 0:
            raise ConfigError(f"weights[{regime}] sum to {total}; at least one must be positive")
        normalized[str(regime)] = {name: value / total for name, value in values.items()}
    return normalized


#: Crypto.com's public market-data MCP server: free, keyless, read-only.
DEFAULT_MARKET_DATA_MCP_URL = "https://mcp.crypto.com/market-data/mcp"

DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://bitcoinmagazine.com/feed",
    "https://cryptoslate.com/feed/",
)

#: Ceilings and floors for the real-time pipeline. These bound *spend and
#: politeness*, not trading risk -- the risk engine is untouched by anything
#: here -- but they are code ceilings for the same reason: a YAML typo must not
#: turn into a thousand Sonnet calls or a rate-limit ban.
PIPELINE_CEILINGS = {"max_escalations_per_day": 200}
PIPELINE_FLOORS = {
    "quote_interval_seconds": 10,
    "account_interval_seconds": 60,
    "news_interval_seconds": 60,
    "sync_interval_seconds": 60,
}

@dataclass(frozen=True)
class PipelineConfig:
    """Cadences, the escalation trigger, and the news sources for ``rhca run``."""

    quote_interval_seconds: int = 60
    account_interval_seconds: int = 600
    news_interval_seconds: int = 120
    sync_interval_seconds: int = 300
    #: The trigger: a candidate goes to System 2 only when it passed every
    #: risk rule *and* clears these, which are meant to sit above the risk
    #: engine's own minimums.
    trigger_min_confidence: Decimal = Decimal("0.5")
    trigger_min_abs_score: Decimal = Decimal("0.3")
    escalation_cooldown_minutes: int = 60
    max_escalations_per_day: int = 24
    system2_model: str = "claude-sonnet-5"
    rss_feeds: tuple[str, ...] = DEFAULT_RSS_FEEDS
    #: A remote MCP server System 2 may query for market data, through the
    #: Anthropic API's MCP connector. Its tools are allowlisted by name in
    #: system2.py; "" turns it off.
    market_data_mcp_url: str = DEFAULT_MARKET_DATA_MCP_URL

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "PipelineConfig":
        data = dict(data or {})
        config = cls()
        for name, value in data.items():
            if not hasattr(config, name):
                raise ConfigError(f"unknown pipeline setting: {name}")
            if value is None:
                continue
            current = getattr(config, name)
            if isinstance(current, tuple):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ConfigError(f"{name} must be a list of strings")
                coerced: Any = tuple(v.strip() for v in value)
            elif isinstance(current, Decimal):
                coerced = to_decimal(value, field=name)
            elif isinstance(current, str):
                coerced = str(value)
            else:
                coerced = int(value)
            config = replace(config, **{name: coerced})
        config.validate()
        return config

    def validate(self) -> None:
        for name, floor in PIPELINE_FLOORS.items():
            if getattr(self, name) < floor:
                raise ConfigError(f"{name}={getattr(self, name)} is below the floor of {floor}")
        for name, ceiling in PIPELINE_CEILINGS.items():
            value = getattr(self, name)
            if not 0 <= value <= ceiling:
                raise ConfigError(f"{name}={value} must be between 0 and {ceiling}")
        if not ZERO <= self.trigger_min_confidence <= Decimal(1):
            raise ConfigError("trigger_min_confidence must be in [0, 1]")
        if not ZERO < self.trigger_min_abs_score <= Decimal(1):
            raise ConfigError("trigger_min_abs_score must be in (0, 1]")
        if self.escalation_cooldown_minutes < 0:
            raise ConfigError("escalation_cooldown_minutes must not be negative")
        for url in self.rss_feeds:
            if not url.startswith("https://"):
                raise ConfigError(f"RSS feeds must be https URLs, got {url!r}")
        if self.market_data_mcp_url and not self.market_data_mcp_url.startswith("https://"):
            raise ConfigError(
                f"market_data_mcp_url must be an https URL or empty, got {self.market_data_mcp_url!r}"
            )


@dataclass(frozen=True)
class AgentConfig:
    """Everything the agent needs to run, assembled from the config directory."""

    execution_mode: ExecutionMode = ExecutionMode.PROPOSE_ONLY
    watchlist: tuple[str, ...] = ("BTC-USD", "ETH-USD")
    rhs_account_number: str | None = None
    risk: RiskLimits = field(default_factory=RiskLimits)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit_log.jsonl"

    @property
    def price_store_path(self) -> Path:
        return self.data_dir / "price_history.jsonl"

    @property
    def kill_switch_path(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    @property
    def proposals_path(self) -> Path:
        return self.data_dir / "proposals.jsonl"

    @property
    def news_path(self) -> Path:
        return self.data_dir / "news.jsonl"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat.json"

    def allows(self, symbol: str) -> bool:
        """Whether ``symbol`` is on the watchlist allowlist."""
        return canonical(symbol) in {canonical(s) for s in self.watchlist}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
        raise ConfigError("PyYAML is required to read YAML config files") from exc
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return loaded


def load_config(
    config_dir: Path | str = DEFAULT_CONFIG_DIR,
    *,
    data_dir: Path | str | None = None,
) -> AgentConfig:
    """Load and validate configuration from ``config_dir``.

    Missing files fall back to the defaults in this module, so a fresh checkout
    is runnable; a *present but invalid* file is an error rather than a silent
    fallback.
    """
    config_dir = Path(config_dir)
    agent_raw = _read_yaml(config_dir / "agent.yaml")
    risk_raw = _read_yaml(config_dir / "risk_limits.yaml")
    strategy_raw = _read_yaml(config_dir / "strategy.yaml")
    pipeline_raw = _read_yaml(config_dir / "pipeline.yaml")

    mode_raw = str(agent_raw.get("execution_mode", ExecutionMode.PROPOSE_ONLY.value)).lower()
    try:
        mode = ExecutionMode(mode_raw)
    except ValueError:
        raise ConfigError(
            f"execution_mode must be one of "
            f"{', '.join(m.value for m in ExecutionMode)}, got {mode_raw!r}"
        ) from None

    # `in` rather than `or`: an explicitly empty watchlist is an error, not a
    # request for the default one. Falling back would re-enable symbols the
    # operator had deliberately removed.
    watchlist_raw = (
        agent_raw["watchlist"] if "watchlist" in agent_raw else list(AgentConfig.watchlist)
    )
    if not isinstance(watchlist_raw, list) or not watchlist_raw:
        raise ConfigError("watchlist must be a non-empty list of pair symbols")
    watchlist = tuple(dict.fromkeys(canonical(str(s)) for s in watchlist_raw))

    account = agent_raw.get("rhs_account_number")
    account = str(account) if account not in (None, "") else None
    # An env var wins so a real account number never has to be committed.
    account = os.environ.get("RHCA_RHS_ACCOUNT_NUMBER") or account

    resolved_data_dir = Path(data_dir or agent_raw.get("data_dir") or DEFAULT_DATA_DIR)

    return AgentConfig(
        execution_mode=mode,
        watchlist=watchlist,
        rhs_account_number=account,
        risk=RiskLimits.from_mapping(risk_raw.get("limits", risk_raw)),
        strategy=StrategyConfig.from_mapping(strategy_raw.get("strategy", strategy_raw)),
        pipeline=PipelineConfig.from_mapping(pipeline_raw.get("pipeline", pipeline_raw)),
        data_dir=resolved_data_dir,
    )
