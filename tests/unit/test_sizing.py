from __future__ import annotations

import pytest

from robinhood_crypto_agent.models import Regime, Side, Signal
from robinhood_crypto_agent.risk.limits import RiskLimits
from robinhood_crypto_agent.sizing.volatility_scaled import size_position
from tests.fixtures.helpers import make_flat_ohlcv, make_trending_ohlcv


def _risk_limits(max_position_usd: float = 100.0) -> RiskLimits:
    return RiskLimits(
        max_position_usd=max_position_usd,
        max_daily_loss_usd=1000.0,
        max_daily_exposure_usd=1000.0,
        max_open_positions=10,
        price_drift_tolerance_pct=1.0,
        allowed_symbols=["BTC"],
    )


def _signal(confidence: float) -> Signal:
    return Signal(
        symbol="BTC",
        regime=Regime.TRENDING,
        composite_value=0.5,
        composite_confidence=confidence,
        side=Side.BUY,
    )


def test_size_never_exceeds_max_position_usd():
    df = make_trending_ohlcv(n=80, daily_return=0.0, noise=0.05, seed=9)
    notional, qty = size_position(
        _signal(1.0), df, reference_price=100.0, risk_limits=_risk_limits(100.0)
    )
    assert notional <= 100.0 + 1e-9
    assert qty * 100.0 == pytest.approx(notional)


def test_lower_confidence_sizes_smaller():
    df = make_flat_ohlcv(n=80)
    notional_low, _ = size_position(_signal(0.2), df, 100.0, _risk_limits(100.0))
    notional_high, _ = size_position(_signal(1.0), df, 100.0, _risk_limits(100.0))
    assert notional_low < notional_high


def test_higher_volatility_sizes_smaller_than_lower_volatility():
    calm = make_flat_ohlcv(n=80)
    volatile = make_trending_ohlcv(n=80, daily_return=0.0, noise=0.05, seed=3)

    notional_calm, _ = size_position(_signal(1.0), calm, 100.0, _risk_limits(100.0))
    notional_volatile, _ = size_position(_signal(1.0), volatile, 100.0, _risk_limits(100.0))
    assert notional_volatile <= notional_calm


def test_zero_reference_price_produces_zero_quantity():
    df = make_flat_ohlcv(n=80)
    _, qty = size_position(_signal(1.0), df, 0.0, _risk_limits(100.0))
    assert qty == 0.0


def test_zero_confidence_produces_zero_size():
    df = make_flat_ohlcv(n=80)
    notional, qty = size_position(_signal(0.0), df, 100.0, _risk_limits(100.0))
    assert notional == 0.0
    assert qty == 0.0
