"""Sizing: the ladder's dollars become a quantity the pair accepts, never more."""

from decimal import Decimal

from robinhood_crypto_agent.config import RiskLimits
from robinhood_crypto_agent.models import PairConstraints, Side
from robinhood_crypto_agent.sizing import size_buy, size_sell

D = Decimal
PAIR = PairConstraints("BTC-USD", D("0.00000001"), min_order_size=D("0.000001"))
LIMITS = RiskLimits(min_notional_per_trade_usd=D("5"), max_notional_per_trade_usd=D("50"))


def test_a_buy_spends_its_dollars_at_the_reference_price_snapped_down():
    result = size_buy("BTC-USD", dollars=D("5"), reference_price=D("65000"), constraints=PAIR,
                      limits=LIMITS)
    assert result.viable and result.side is Side.BUY
    assert result.quantity == D("0.00007692")  # 5 / 65000, rounded down to the increment
    assert result.notional == D("5.00")
    assert result.quantity * D("65000") <= D("5")


def test_a_step_under_the_minimum_trade_is_refused():
    result = size_buy("BTC-USD", dollars=D("4"), reference_price=D("100"), constraints=PAIR,
                      limits=LIMITS)
    assert not result.viable and "minimum trade size" in result.rejected_reason


def test_sizing_never_shrinks_a_step_to_fit_a_cap():
    """A step over the per-trade cap is proposed at its size, and risk blocks it."""
    tight = RiskLimits(min_notional_per_trade_usd=D("5"), max_notional_per_trade_usd=D("10"))
    result = size_buy("BTC-USD", dollars=D("20"), reference_price=D("100"), constraints=PAIR,
                      limits=tight)
    assert result.notional == D("20.00")


def test_the_pair_minimums_bind():
    coarse = PairConstraints("BTC-USD", D("0.01"), min_order_size=D("0.1"))
    result = size_buy("BTC-USD", dollars=D("5"), reference_price=D("65000"), constraints=coarse,
                      limits=LIMITS)
    assert not result.viable and "rounds to zero" in result.rejected_reason
    small = size_sell("BTC-USD", quantity=D("0.05"), reference_price=D("100"), constraints=coarse)
    assert not small.viable and "minimum order size" in small.rejected_reason


def test_a_sell_keeps_its_quantity_snapped_down_with_no_trade_minimum():
    """Closing a position is never refused for being small: that would strand it."""
    result = size_sell("BTC-USD", quantity=D("0.000054321"), reference_price=D("65000"),
                       constraints=PAIR)
    assert result.viable and result.side is Side.SELL
    assert result.quantity == D("0.00005432")
    assert result.notional < D("5")


def test_a_bad_price_is_refused():
    assert not size_sell("BTC-USD", quantity=D("1"), reference_price=D("0"),
                         constraints=PAIR).viable
    assert not size_buy("BTC-USD", dollars=D("5"), reference_price=D("0"), constraints=PAIR,
                        limits=LIMITS).viable
