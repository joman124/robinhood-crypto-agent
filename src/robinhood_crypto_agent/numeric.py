"""Decimal helpers.

Money and crypto quantities are :class:`~decimal.Decimal` throughout. The
RobinHood MCP order tools take and return *decimal strings*, and binary floats
cannot represent them exactly -- a float round-trip is how you end up sending
``0.30000000000000004`` as a quantity. Nothing in this package converts a price
or a quantity to ``float``; the only floats are indicator intermediates, which
are statistics rather than order fields.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .errors import AgentError

ZERO = Decimal("0")


def to_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Coerce ``value`` to :class:`Decimal` without going through ``float``.

    Accepts the decimal strings the MCP tools return, ints, and Decimals.
    Floats are accepted but routed through ``repr`` so the caller gets the
    shortest representation rather than the full binary expansion.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):  # bool is an int subclass; almost never intended
        raise AgentError(f"{field}: expected a number, got a bool")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise AgentError(f"{field}: expected a number, got an empty string")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise AgentError(f"{field}: {value!r} is not a valid decimal") from exc
    else:
        raise AgentError(f"{field}: expected a number, got {type(value).__name__}")

    if not result.is_finite():
        raise AgentError(f"{field}: {value!r} is not finite")
    return result


def opt_decimal(value: Any, *, field: str = "value") -> Decimal | None:
    """Like :func:`to_decimal`, but ``None`` passes through."""
    if value is None:
        return None
    return to_decimal(value, field=field)


def quantize_to_increment(
    value: Decimal, increment: Decimal, *, rounding: str = ROUND_DOWN
) -> Decimal:
    """Snap ``value`` down to a multiple of ``increment``.

    Robinhood rejects an order whose quantity is not a multiple of the pair's
    ``min_order_quantity_increment``. Rounding *down* by default matters: it can
    only ever make an order smaller, so snapping never silently pushes a
    position past a risk limit that was checked against the unrounded size.
    """
    if increment <= ZERO:
        raise AgentError(f"increment must be positive, got {increment}")
    steps = (value / increment).to_integral_value(rounding=rounding)
    return (steps * increment).normalize() + ZERO


def round_money(value: Decimal, places: int = 2) -> Decimal:
    """Round a USD amount to ``places`` decimals, half-up like an invoice."""
    exponent = Decimal(1).scaleb(-places)
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal) -> str:
    """Render a Decimal as a plain string with no exponent.

    ``str(Decimal("1E-8"))`` is ``"1E-8"``, which the order API will not accept.
    """
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return f"{normalized:f}"


def format_money(value: Decimal, places: int = 2) -> str:
    """Render a USD amount with a fixed number of decimals.

    Distinct from :func:`format_decimal`, which normalizes trailing zeros --
    correct for a quantity, wrong for money, where "$20" and "$20.00" should
    not render differently depending on the arithmetic that produced them.
    """
    return f"{round_money(value, places):f}"


def pct_change(new: Decimal, old: Decimal) -> Decimal:
    """Percentage change from ``old`` to ``new``, as a percentage (not a ratio)."""
    if old == ZERO:
        raise AgentError("cannot compute a percentage change from zero")
    return (new - old) / old * Decimal(100)


def abs_pct_drift(new: Decimal, old: Decimal) -> Decimal:
    """Absolute percentage drift between two prices."""
    return abs(pct_change(new, old))
