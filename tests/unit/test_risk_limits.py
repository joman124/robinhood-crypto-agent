from __future__ import annotations

import pytest
from pydantic import ValidationError

from robinhood_crypto_agent.risk.limits import (
    ABSOLUTE_MAX_DAILY_EXPOSURE_USD,
    ABSOLUTE_MAX_DAILY_LOSS_USD,
    ABSOLUTE_MAX_OPEN_POSITIONS,
    ABSOLUTE_MAX_POSITION_USD,
    RiskLimits,
)


def _base_kwargs(**overrides):
    kwargs = dict(
        max_position_usd=50.0,
        max_daily_loss_usd=100.0,
        max_daily_exposure_usd=250.0,
        max_open_positions=3,
        price_drift_tolerance_pct=0.5,
        allowed_symbols=["BTC"],
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_limits_accepted():
    limits = RiskLimits(**_base_kwargs())
    assert limits.max_position_usd == 50.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_position_usd", ABSOLUTE_MAX_POSITION_USD + 1),
        ("max_daily_loss_usd", ABSOLUTE_MAX_DAILY_LOSS_USD + 1),
        ("max_daily_exposure_usd", ABSOLUTE_MAX_DAILY_EXPOSURE_USD + 1),
        ("max_open_positions", ABSOLUTE_MAX_OPEN_POSITIONS + 1),
    ],
)
def test_exceeding_absolute_ceiling_rejected(field, value):
    with pytest.raises(ValidationError):
        RiskLimits(**_base_kwargs(**{field: value}))


def test_exactly_at_ceiling_is_accepted():
    limits = RiskLimits(**_base_kwargs(max_position_usd=ABSOLUTE_MAX_POSITION_USD))
    assert limits.max_position_usd == ABSOLUTE_MAX_POSITION_USD
