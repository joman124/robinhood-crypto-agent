"""The contract layer is the last line of defence before a live order.

Each test here names a rule from the published RobinHood MCP tool schema and
asserts that a payload violating it is refused offline.
"""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.errors import ContractViolation
from robinhood_crypto_agent.mcp.contract import (
    ORDER_COLLARS,
    validate_crypto_order_args,
    validate_ref_id,
    validate_rhs_account_number,
    worst_case_notional,
)
from robinhood_crypto_agent.models import Side


def base(**overrides):
    payload = {
        "rhs_account_number": "123456789",
        "symbol": "BTC-USD",
        "side": "buy",
        "type": "limit",
        "quantity": "0.001",
        "limit_price": "50000",
        "time_in_force": "gtc",
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def test_valid_limit_order_passes():
    assert validate_crypto_order_args(base()) is not None


def test_alphanumeric_account_number_is_rejected():
    """The order tools take rhs_account_number, not the alphanumeric one."""
    with pytest.raises(ContractViolation, match="numeric account number"):
        validate_crypto_order_args(base(rhs_account_number="RH12345678"))


def test_missing_account_number_is_rejected():
    with pytest.raises(ContractViolation, match="rhs_account_number"):
        validate_crypto_order_args(base(rhs_account_number=""))


def test_quantity_and_dollar_amount_are_mutually_exclusive():
    with pytest.raises(ContractViolation, match="both were set"):
        validate_crypto_order_args(base(dollar_amount="100"))
    with pytest.raises(ContractViolation, match="neither was set"):
        validate_crypto_order_args(base(quantity=None))


def test_dollar_amount_alone_is_valid():
    payload = base(quantity=None, dollar_amount="100")
    assert validate_crypto_order_args(payload) is not None


def test_limit_price_required_for_limit_and_stop_limit():
    with pytest.raises(ContractViolation, match="limit_price is required"):
        validate_crypto_order_args(base(limit_price=None))
    with pytest.raises(ContractViolation, match="limit_price is required"):
        validate_crypto_order_args(base(type="stop_limit", limit_price=None, stop_price="49000"))


def test_limit_price_rejected_on_market_order():
    with pytest.raises(ContractViolation, match="not accepted"):
        validate_crypto_order_args(base(type="market"))


def test_stop_price_required_for_stop_types():
    with pytest.raises(ContractViolation, match="stop_price is required"):
        validate_crypto_order_args(
            base(type="stop_loss", limit_price=None, time_in_force="gfd")
        )


def test_ioc_is_never_valid_for_crypto():
    with pytest.raises(ContractViolation, match="'ioc' is never supported"):
        validate_crypto_order_args(base(time_in_force="ioc"))


def test_market_and_limit_accept_only_gtc():
    with pytest.raises(ContractViolation, match="not valid for a limit order"):
        validate_crypto_order_args(base(time_in_force="gfd"))
    with pytest.raises(ContractViolation, match="not valid for a market order"):
        validate_crypto_order_args(
            base(type="market", limit_price=None, time_in_force="gfw")
        )


def test_stop_types_accept_bounded_durations():
    for tif in ("gtc", "gfd", "gfw", "gfm"):
        payload = base(type="stop_loss", limit_price=None, stop_price="49000", time_in_force=tif)
        assert validate_crypto_order_args(payload) is not None


def test_ref_id_must_be_a_uuid():
    with pytest.raises(ContractViolation, match="must be a UUID"):
        validate_crypto_order_args(base(ref_id="retry-1"))
    payload = base(ref_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301")
    assert validate_crypto_order_args(payload) is not None


def test_unknown_arguments_are_rejected():
    with pytest.raises(ContractViolation, match="unknown order argument"):
        validate_crypto_order_args(base(account_number="RH1"))


def test_negative_and_zero_sizes_are_rejected():
    with pytest.raises(ContractViolation, match="quantity must be positive"):
        validate_crypto_order_args(base(quantity="0"))
    with pytest.raises(ContractViolation, match="dollar_amount must be positive"):
        validate_crypto_order_args(base(quantity=None, dollar_amount="-5"))


class TestTaxLots:
    def lots(self, **overrides):
        payload = base(side="sell", quantity="0.5")
        payload["tax_lots"] = overrides.pop(
            "tax_lots",
            [{"open_lot_id": "a", "quantity": "0.3"}, {"open_lot_id": "b", "quantity": "0.2"}],
        )
        payload.update(overrides)
        return payload

    def test_valid_lots_pass(self):
        assert validate_crypto_order_args(self.lots()) is not None

    def test_lots_are_sell_only(self):
        with pytest.raises(ContractViolation, match="only be set on a sell"):
            validate_crypto_order_args(self.lots(side="buy"))

    def test_lots_require_quantity_not_dollar_amount(self):
        payload = self.lots()
        payload.pop("quantity")
        payload["dollar_amount"] = "100"
        with pytest.raises(ContractViolation, match="quantity-based order"):
            validate_crypto_order_args(payload)

    def test_lot_quantities_must_sum_to_order_quantity(self):
        with pytest.raises(ContractViolation, match="sum to"):
            validate_crypto_order_args(
                self.lots(tax_lots=[{"open_lot_id": "a", "quantity": "0.4"}])
            )

    def test_lot_precision_is_capped_at_eight_places(self):
        with pytest.raises(ContractViolation, match="8 decimal places"):
            validate_crypto_order_args(
                self.lots(
                    quantity="0.123456789",
                    tax_lots=[{"open_lot_id": "a", "quantity": "0.123456789"}],
                )
            )

    def test_too_many_lots_rejected(self):
        lots = [{"open_lot_id": str(i), "quantity": "0.01"} for i in range(51)]
        with pytest.raises(ContractViolation, match="at most 50"):
            validate_crypto_order_args(self.lots(quantity="0.51", tax_lots=lots))


def test_worst_case_notional_applies_the_collar():
    """A dollar market buy can cost ~1% more; a sell can return ~5% less."""
    assert ORDER_COLLARS[Side.BUY] == Decimal("0.01")
    assert ORDER_COLLARS[Side.SELL] == Decimal("0.05")
    assert worst_case_notional(Decimal("100"), Side.BUY) == Decimal("101.00")


def test_helpers_are_directly_usable():
    assert validate_rhs_account_number(" 42 ") == "42"
    assert validate_ref_id("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
