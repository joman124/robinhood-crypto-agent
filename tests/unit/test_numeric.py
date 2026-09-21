from decimal import ROUND_UP, Decimal

import pytest

from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.numeric import (
    abs_pct_drift,
    format_decimal,
    pct_change,
    quantize_to_increment,
    round_money,
    to_decimal,
)


def test_to_decimal_avoids_float_expansion():
    """A float must not round-trip into its full binary expansion."""
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal("0.00000001") == Decimal("1E-8")


def test_to_decimal_rejects_bools_and_junk():
    with pytest.raises(AgentError):
        to_decimal(True)
    with pytest.raises(AgentError):
        to_decimal("not-a-number")
    with pytest.raises(AgentError):
        to_decimal("")
    with pytest.raises(AgentError):
        to_decimal(float("inf"))


def test_format_decimal_never_uses_exponent_notation():
    """Order fields are decimal strings; 1E-8 would be rejected by the API."""
    assert format_decimal(Decimal("1E-8")) == "0.00000001"
    assert format_decimal(Decimal("1E+3")) == "1000"
    assert format_decimal(Decimal("0.30000000000000004")) == "0.30000000000000004"


def test_quantize_rounds_down_by_default():
    """Snapping to an increment must never make an order bigger."""
    assert quantize_to_increment(Decimal("0.123456789"), Decimal("0.00000001")) == Decimal(
        "0.12345678"
    )
    assert quantize_to_increment(Decimal("1.9"), Decimal("1")) == Decimal("1")
    assert quantize_to_increment(Decimal("1.1"), Decimal("1"), rounding=ROUND_UP) == Decimal("2")


def test_quantize_rejects_non_positive_increment():
    with pytest.raises(AgentError):
        quantize_to_increment(Decimal("1"), Decimal("0"))


def test_pct_change_and_drift():
    assert pct_change(Decimal("110"), Decimal("100")) == Decimal("10")
    assert abs_pct_drift(Decimal("90"), Decimal("100")) == Decimal("10")
    with pytest.raises(AgentError):
        pct_change(Decimal("1"), Decimal("0"))


def test_round_money():
    assert round_money(Decimal("54.450250731")) == Decimal("54.45")
    assert round_money(Decimal("0.005")) == Decimal("0.01")
