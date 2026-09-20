"""The RobinHood MCP crypto tool contract, encoded as checkable rules.

Everything here mirrors the published tool schemas of the ``RobinHood`` MCP
server. It exists because the failure mode this repo most needs to avoid is a
*plausible-looking but invalid* order payload: one where ``account_number`` was
passed where ``rhs_account_number`` was wanted, or a ``limit`` order carried
``time_in_force: "ioc"`` (never supported for crypto), or both ``quantity`` and
``dollar_amount`` were set. Those are rejected here, offline, with a message
naming the rule -- rather than at Robinhood, against a live account.

The rules encoded below, and where they come from:

* Order tools take ``rhs_account_number`` -- the *numeric* field on a
  ``get_accounts`` entry -- not the alphanumeric ``account_number`` that
  ``get_portfolio`` takes.
* Exactly one of ``quantity`` or ``dollar_amount``.
* ``limit_price`` is required for ``limit`` and ``stop_limit``;
  ``stop_price`` is required for ``stop_loss`` and ``stop_limit``.
* ``market`` and ``limit`` support only ``gtc``. Stop types additionally
  support ``gfd``/``gfw``/``gfm``. ``ioc`` is never valid for crypto.
* ``ref_id`` is an idempotency key: one UUID per logical order, re-sent
  verbatim when retrying a transient failure.
* ``tax_lots`` is sell-only, quantity-only, at most 50 lots, and the lot
  quantities must sum to the order quantity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from ..errors import ContractViolation
from ..models import OrderType, Side, TimeInForce
from ..numeric import to_decimal

#: Tool names, so a typo is a NameError here rather than a silent no-op later.
CRYPTO_TOOLS = {
    "accounts": "get_accounts",
    "portfolio": "get_portfolio",
    "currency_pairs": "get_currency_pairs",
    "quotes": "get_crypto_quotes",
    "positions": "get_crypto_positions",
    "orders": "get_crypto_orders",
    "preview": "preview_crypto_order",
    "place": "place_crypto_order",
    "cancel": "cancel_crypto_order",
}

#: The only tool in this package's vocabulary that moves money.
MUTATING_TOOLS = frozenset({CRYPTO_TOOLS["place"], CRYPTO_TOOLS["cancel"]})

#: Time-in-force values each order type accepts.
ALLOWED_TIF: dict[OrderType, frozenset[TimeInForce]] = {
    OrderType.MARKET: frozenset({TimeInForce.GTC}),
    OrderType.LIMIT: frozenset({TimeInForce.GTC}),
    OrderType.STOP_LOSS: frozenset(
        {TimeInForce.GTC, TimeInForce.GFD, TimeInForce.GFW, TimeInForce.GFM}
    ),
    OrderType.STOP_LIMIT: frozenset(
        {TimeInForce.GTC, TimeInForce.GFD, TimeInForce.GFW, TimeInForce.GFM}
    ),
}

#: Order types that require ``limit_price`` / ``stop_price``.
REQUIRES_LIMIT_PRICE = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})
REQUIRES_STOP_PRICE = frozenset({OrderType.STOP_LOSS, OrderType.STOP_LIMIT})

#: Worst-case slippage collars on a ``dollar_amount`` market order, as ratios.
#:
#: A dollar-sized market buy can cost up to ~1% more than requested, and a
#: dollar-sized market sell can return up to ~5% less. The risk engine sizes
#: against the worst case rather than the nominal, so a cap checked before
#: submission still holds after a collar-width move.
ORDER_COLLARS = {
    Side.BUY: Decimal("0.01"),
    Side.SELL: Decimal("0.05"),
}

MAX_TAX_LOTS = 50
MAX_LOT_DECIMAL_PLACES = 8


@dataclass(frozen=True)
class ToolContract:
    """Describes one tool for reporting and for the ``describe-tools`` command."""

    name: str
    mutating: bool
    summary: str


TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(CRYPTO_TOOLS["accounts"], False, "Resolve account_number and rhs_account_number."),
    ToolContract(CRYPTO_TOOLS["portfolio"], False, "Portfolio value and buying power (account_number)."),
    ToolContract(CRYPTO_TOOLS["currency_pairs"], False, "Per-pair increments, limits, and halts."),
    ToolContract(CRYPTO_TOOLS["quotes"], False, "Live bid/ask/mark plus previous close."),
    ToolContract(CRYPTO_TOOLS["positions"], False, "Open positions and cost basis."),
    ToolContract(CRYPTO_TOOLS["orders"], False, "Order history and live order state."),
    ToolContract(CRYPTO_TOOLS["preview"], False, "Simulate an order; resolves fees and validation."),
    ToolContract(CRYPTO_TOOLS["place"], True, "Places a REAL order with REAL money."),
    ToolContract(CRYPTO_TOOLS["cancel"], True, "Cancels an open order."),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def _coerce_enum(value: Any, enum_cls: type, field: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).lower())
    except ValueError:
        allowed = ", ".join(sorted(m.value for m in enum_cls))
        raise ContractViolation(f"{field}: {value!r} is not one of {{{allowed}}}") from None


def validate_rhs_account_number(value: Any) -> str:
    """Check that the *numeric* account number was passed to an order tool.

    ``get_accounts`` returns both an alphanumeric ``account_number`` and a
    numeric ``rhs_account_number``; only the latter is valid here. Anything
    containing a non-digit is almost certainly the other field.
    """
    text = str(value).strip()
    _require(bool(text), "rhs_account_number is required for crypto order tools")
    _require(
        text.isdigit(),
        f"rhs_account_number must be the numeric account number, got {text!r} -- "
        "this looks like the alphanumeric 'account_number', which the order tools reject",
    )
    return text


def validate_ref_id(value: Any) -> str:
    """Check that ``ref_id`` is a UUID, since it is the idempotency key."""
    text = str(value).strip()
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        raise ContractViolation(f"ref_id must be a UUID, got {text!r}") from None
    return text


def _validate_tax_lots(lots: Iterable[Any], *, side: Side, quantity: Decimal | None) -> None:
    lots = list(lots)
    _require(side is Side.SELL, "tax_lots may only be set on a sell order")
    _require(
        quantity is not None,
        "tax_lots requires a quantity-based order; it cannot be combined with dollar_amount",
    )
    _require(bool(lots), "tax_lots was provided but empty")
    _require(len(lots) <= MAX_TAX_LOTS, f"tax_lots accepts at most {MAX_TAX_LOTS} lots, got {len(lots)}")

    total = Decimal(0)
    for index, lot in enumerate(lots):
        _require(isinstance(lot, dict), f"tax_lots[{index}] must be an object")
        _require("open_lot_id" in lot, f"tax_lots[{index}] is missing open_lot_id")
        _require("quantity" in lot, f"tax_lots[{index}] is missing quantity")
        lot_quantity = to_decimal(lot["quantity"], field=f"tax_lots[{index}].quantity")
        _require(lot_quantity > 0, f"tax_lots[{index}].quantity must be positive")
        _require(
            -lot_quantity.as_tuple().exponent <= MAX_LOT_DECIMAL_PLACES,
            f"tax_lots[{index}].quantity takes at most {MAX_LOT_DECIMAL_PLACES} decimal places",
        )
        total += lot_quantity

    assert quantity is not None  # guarded above
    _require(
        total == quantity,
        f"tax_lots quantities sum to {total}, which must equal the order quantity {quantity}",
    )


def validate_crypto_order_args(args: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``preview_crypto_order``/``place_crypto_order`` payload.

    Returns the payload unchanged on success so it can be used inline. Raises
    :class:`ContractViolation` naming the specific rule otherwise.
    """
    _require(isinstance(args, dict), "order arguments must be a mapping")

    unknown = set(args) - {
        "rhs_account_number",
        "symbol",
        "side",
        "type",
        "quantity",
        "dollar_amount",
        "limit_price",
        "stop_price",
        "time_in_force",
        "ref_id",
        "tax_lots",
    }
    _require(not unknown, f"unknown order argument(s): {', '.join(sorted(unknown))}")

    validate_rhs_account_number(args.get("rhs_account_number"))

    symbol = str(args.get("symbol", "")).strip()
    _require(bool(symbol), "symbol is required")

    side = _coerce_enum(args.get("side"), Side, "side")
    order_type = _coerce_enum(args.get("type"), OrderType, "type")

    has_quantity = args.get("quantity") is not None
    has_dollar = args.get("dollar_amount") is not None
    _require(
        has_quantity != has_dollar,
        "provide exactly one of quantity or dollar_amount"
        + (" (both were set)" if has_quantity and has_dollar else " (neither was set)"),
    )

    quantity = to_decimal(args["quantity"], field="quantity") if has_quantity else None
    if quantity is not None:
        _require(quantity > 0, f"quantity must be positive, got {quantity}")
    if has_dollar:
        dollar_amount = to_decimal(args["dollar_amount"], field="dollar_amount")
        _require(dollar_amount > 0, f"dollar_amount must be positive, got {dollar_amount}")

    has_limit = args.get("limit_price") is not None
    has_stop = args.get("stop_price") is not None

    if order_type in REQUIRES_LIMIT_PRICE:
        _require(has_limit, f"limit_price is required for a {order_type.value} order")
        _require(
            to_decimal(args["limit_price"], field="limit_price") > 0,
            "limit_price must be positive",
        )
    else:
        _require(
            not has_limit,
            f"limit_price is not accepted on a {order_type.value} order",
        )

    if order_type in REQUIRES_STOP_PRICE:
        _require(has_stop, f"stop_price is required for a {order_type.value} order")
        _require(
            to_decimal(args["stop_price"], field="stop_price") > 0,
            "stop_price must be positive",
        )
    else:
        _require(not has_stop, f"stop_price is not accepted on a {order_type.value} order")

    if args.get("time_in_force") is not None:
        raw_tif = str(args["time_in_force"]).lower()
        _require(
            raw_tif != "ioc",
            "time_in_force 'ioc' is never supported for crypto orders",
        )
        tif = _coerce_enum(raw_tif, TimeInForce, "time_in_force")
        allowed = ALLOWED_TIF[order_type]
        _require(
            tif in allowed,
            f"time_in_force {tif.value!r} is not valid for a {order_type.value} order "
            f"(allowed: {', '.join(sorted(t.value for t in allowed))})",
        )

    if args.get("ref_id") is not None:
        validate_ref_id(args["ref_id"])

    if args.get("tax_lots") is not None:
        _validate_tax_lots(args["tax_lots"], side=side, quantity=quantity)

    return args


def worst_case_notional(dollar_amount: Decimal, side: Side) -> Decimal:
    """Worst-case cash effect of a dollar-sized market order, after its collar.

    Used by the risk engine so that a notional cap is checked against what the
    order could actually cost, not what it nominally requested.
    """
    return dollar_amount * (Decimal(1) + ORDER_COLLARS[side])
