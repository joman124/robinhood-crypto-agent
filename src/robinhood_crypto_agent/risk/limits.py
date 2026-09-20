from __future__ import annotations

from pydantic import BaseModel, field_validator

# Absolute ceilings a config/risk_limits.yaml edit can never exceed - defense
# in depth against a careless config change. See docs/risk-controls.md.
ABSOLUTE_MAX_POSITION_USD = 500.0
ABSOLUTE_MAX_DAILY_LOSS_USD = 1000.0
ABSOLUTE_MAX_DAILY_EXPOSURE_USD = 2500.0
ABSOLUTE_MAX_OPEN_POSITIONS = 10


class RiskLimits(BaseModel):
    max_position_usd: float
    max_daily_loss_usd: float
    max_daily_exposure_usd: float
    max_open_positions: int
    price_drift_tolerance_pct: float
    allowed_symbols: list[str]

    @field_validator("max_position_usd")
    @classmethod
    def _position_below_ceiling(cls, v: float) -> float:
        if v > ABSOLUTE_MAX_POSITION_USD:
            raise ValueError(
                f"max_position_usd {v} exceeds absolute ceiling {ABSOLUTE_MAX_POSITION_USD}"
            )
        return v

    @field_validator("max_daily_loss_usd")
    @classmethod
    def _loss_below_ceiling(cls, v: float) -> float:
        if v > ABSOLUTE_MAX_DAILY_LOSS_USD:
            raise ValueError(
                f"max_daily_loss_usd {v} exceeds absolute ceiling {ABSOLUTE_MAX_DAILY_LOSS_USD}"
            )
        return v

    @field_validator("max_daily_exposure_usd")
    @classmethod
    def _exposure_below_ceiling(cls, v: float) -> float:
        if v > ABSOLUTE_MAX_DAILY_EXPOSURE_USD:
            raise ValueError(
                "max_daily_exposure_usd "
                f"{v} exceeds absolute ceiling {ABSOLUTE_MAX_DAILY_EXPOSURE_USD}"
            )
        return v

    @field_validator("max_open_positions")
    @classmethod
    def _positions_below_ceiling(cls, v: int) -> int:
        if v > ABSOLUTE_MAX_OPEN_POSITIONS:
            raise ValueError(
                f"max_open_positions {v} exceeds absolute ceiling {ABSOLUTE_MAX_OPEN_POSITIONS}"
            )
        return v
