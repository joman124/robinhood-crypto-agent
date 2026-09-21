"""Sizing: every factor may only shrink the position."""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.config import RiskLimits, StrategyConfig
from robinhood_crypto_agent.models import (
    CompositeView,
    Direction,
    PairConstraints,
    Position,
    Regime,
    Side,
)
from robinhood_crypto_agent.sizing import size_position
from tests.conftest import make_candles, uptrend


def view(score=0.8, confidence=0.9, direction=Direction.LONG):
    return CompositeView(
        symbol="BTC-USD",
        regime=Regime.TRENDING,
        score=score,
        confidence=confidence,
        direction=direction,
        signals=[],
        weights={},
    )


def size(view_obj, *, limits=None, constraints=None, price="100", **kwargs):
    return size_position(
        view_obj,
        reference_price=Decimal(price),
        candles=make_candles(uptrend()),
        limits=limits or RiskLimits(),
        strategy=StrategyConfig(),
        constraints=constraints
        or PairConstraints("BTC-USD", Decimal("0.00000001"), min_order_size=Decimal("0.000001")),
        **kwargs,
    )


def test_size_scales_with_conviction():
    strong = size(view(score=1.0, confidence=1.0))
    weak = size(view(score=0.4, confidence=0.5))
    assert strong.notional > weak.notional


def test_size_never_exceeds_the_per_trade_cap():
    limits = RiskLimits(max_notional_per_trade_usd=Decimal("100"))
    result = size(view(score=1.0, confidence=1.0), limits=limits)
    assert result.notional <= Decimal("100")


def test_concentration_cap_applies():
    result = size(view(score=1.0, confidence=1.0), portfolio_value=Decimal("200"))
    assert result.notional <= Decimal("20")  # 10% of 200
    assert result.detail["concentration_cap"] == "20.00"


def test_below_minimum_notional_is_rejected_not_rounded_up():
    result = size(view(score=0.05, confidence=0.1))
    assert not result.viable
    assert "minimum trade size" in result.rejected_reason


def test_flat_view_is_not_sized():
    result = size(view(direction=Direction.FLAT))
    assert not result.viable
    assert "flat" in result.rejected_reason


def test_quantity_is_snapped_down_to_the_increment():
    constraints = PairConstraints("BTC-USD", Decimal("0.001"))
    result = size(view(score=1.0, confidence=1.0), constraints=constraints, price="97")
    assert result.quantity % Decimal("0.001") == 0
    assert result.quantity * Decimal("97") <= RiskLimits().max_notional_per_trade_usd


def test_coarse_increment_rejects_rather_than_rounding_up():
    constraints = PairConstraints("BTC-USD", Decimal("1"))
    result = size(view(), constraints=constraints, price="1000000")
    assert not result.viable
    assert "rounds to zero" in result.rejected_reason


def test_pair_minimum_order_size_is_respected():
    constraints = PairConstraints(
        "BTC-USD", Decimal("0.00000001"), min_order_size=Decimal("10")
    )
    result = size(view(), constraints=constraints, price="1000")
    assert not result.viable
    assert "minimum order size" in result.rejected_reason


def test_pair_maximum_order_size_caps_quantity():
    constraints = PairConstraints(
        "BTC-USD", Decimal("0.001"), max_order_size=Decimal("0.005")
    )
    result = size(view(score=1.0, confidence=1.0), constraints=constraints, price="100")
    assert result.quantity <= Decimal("0.005")


def test_pair_minimum_notional_is_respected():
    constraints = PairConstraints(
        "BTC-USD", Decimal("0.00000001"), min_notional=Decimal("500")
    )
    result = size(view(), constraints=constraints)
    assert not result.viable
    assert "below the pair minimum" in result.rejected_reason


def test_sell_without_a_position_is_refused():
    """This agent is spot-only; a sell with no holding would be a short."""
    result = size(view(score=-0.8, direction=Direction.SHORT))
    assert not result.viable
    assert "does not short" in result.rejected_reason


def test_sell_is_capped_at_the_held_quantity():
    result = size(
        view(score=-1.0, confidence=1.0, direction=Direction.SHORT),
        position=Position("BTC-USD", Decimal("0.01")),
        price="100",
    )
    assert result.side is Side.SELL
    assert result.quantity <= Decimal("0.01")


def test_high_volatility_cuts_size():
    calm = [100 * (1.001**i) for i in range(60)]
    wild = [100 * (1.05 if i % 2 else 0.96) ** 1 * (1.001**i) for i in range(60)]
    limits, strategy = RiskLimits(), StrategyConfig()
    constraints = PairConstraints("BTC-USD", Decimal("0.00000001"))

    def notional(prices):
        return size_position(
            view(score=1.0, confidence=1.0),
            reference_price=Decimal("100"),
            candles=make_candles(prices),
            limits=limits,
            strategy=strategy,
            constraints=constraints,
        ).notional

    assert notional(wild) < notional(calm)


def test_zero_reference_price_is_rejected():
    result = size(view(), price="0")
    assert not result.viable
    assert "positive" in result.rejected_reason


@pytest.mark.parametrize("score,confidence", [(0.0, 0.9), (0.9, 0.0)])
def test_zero_conviction_is_rejected(score, confidence):
    result = size(view(score=score, confidence=confidence))
    assert not result.viable
