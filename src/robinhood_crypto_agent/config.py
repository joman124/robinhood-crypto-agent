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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .models import ExecutionMode, parse_timestamp
from .numeric import ZERO, to_decimal
from .strategy.ladder import DEFAULT_STEPS, MODE_ANCHOR, Ladder, parse_steps, trend_bars
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
        ):
            if name in data and data[name] is not None:
                limits = replace(limits, **{name: to_decimal(data[name], field=name)})
        for name in ("max_open_positions", "max_quote_age_seconds"):
            if name in data and data[name] is not None:
                limits = replace(limits, **{name: int(data[name])})

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


@dataclass(frozen=True)
class StrategyConfig:
    """The trend ladder's settings (``strategy.ladder``), and the bar interval.

    The agent trades the ladder in ``anchor`` mode; ``lot`` mode exists only as
    a comparison row in ``rhca backtest``.
    """

    bar_interval_minutes: int = 60
    #: (percent under the anchor, dollars) per step, in increasing order.
    steps: tuple[tuple[Decimal, Decimal], ...] = DEFAULT_STEPS
    #: Buy only on a close above the average of this many days of closes.
    #: 0 turns the filter off (and with it the trend exit).
    trend_days: int = 50
    #: Sell everything on a close at or under that average.
    trend_exit: bool = True
    #: While the agent holds none of a coin, a dip is measured from the highest
    #: close since this moment or since its last ladder position was sold,
    #: whichever is later. ``None`` reads every stored bar.
    anchor_since: datetime | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "StrategyConfig":
        data = dict(data or {})
        config = cls()
        for name, value in data.items():
            if not hasattr(config, name):
                raise ConfigError(f"unknown strategy setting: {name}")
            if value is None:
                continue
            try:
                coerced = _coerce_strategy(name, value)
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise ConfigError(f"strategy setting {name}: {exc}") from exc
            config = replace(config, **{name: coerced})
        config.validate()
        return config

    def validate(self) -> None:
        if self.bar_interval_minutes < 1:
            raise ConfigError("bar_interval_minutes must be at least 1")
        if self.trend_days < 0:
            raise ConfigError("trend_days must be 0 (off) or a number of days")
        try:
            self.ladder()
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def ladder(self) -> Ladder:
        """The rule the agent trades."""
        filtered = self.trend_days > 0
        return Ladder(
            steps=self.steps,
            mode=MODE_ANCHOR,
            trend_filter=filtered,
            trend_exit=filtered and self.trend_exit,
        )

    @property
    def trend_bars(self) -> int:
        """Bars in the trend average; 0 when the filter is off."""
        return trend_bars(self.trend_days, self.bar_interval_minutes) if self.trend_days else 0

    @property
    def required_bars(self) -> int:
        """Closed bars needed before the rule can say anything."""
        return max(1, self.trend_bars)

    @property
    def history_days(self) -> int:
        """Days of bars to keep on hand: the trend window and two days' slack."""
        return self.required_bars * self.bar_interval_minutes // (24 * 60) + 2


def _coerce_strategy(name: str, value: Any) -> Any:
    if name == "steps":
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        return parse_steps(str(value))
    if name == "trend_exit":
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return value
    if name == "anchor_since":
        if isinstance(value, datetime):
            return parse_timestamp(value)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return parse_timestamp(str(value))
    return int(value)


#: Floors for the real-time loop's cadences. They bound politeness, not
#: trading risk: a YAML typo must not turn into a rate-limit ban.
PIPELINE_FLOORS = {
    "quote_interval_seconds": 10,
    "account_interval_seconds": 60,
    "sync_interval_seconds": 60,
}


@dataclass(frozen=True)
class PipelineConfig:
    """Cadences for ``rhca run``."""

    quote_interval_seconds: int = 60
    account_interval_seconds: int = 600
    sync_interval_seconds: int = 300

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "PipelineConfig":
        data = dict(data or {})
        config = cls()
        for name, value in data.items():
            if not hasattr(config, name):
                raise ConfigError(f"unknown pipeline setting: {name}")
            if value is None:
                continue
            config = replace(config, **{name: int(value)})
        config.validate()
        return config

    def validate(self) -> None:
        for name, floor in PIPELINE_FLOORS.items():
            if getattr(self, name) < floor:
                raise ConfigError(f"{name}={getattr(self, name)} is below the floor of {floor}")


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
