"""Sizing: the rule's dollars in, an order quantity the pair accepts out.

The split decides *how much* -- a breakout entry's dollars from its risk, a
tranche's from its share, a stop's quantity from what the sleeve holds -- so
sizing only turns that into a quantity: divided by the price the order would
cross, snapped *down* to the pair's increment, and checked against the pair's
minimums. It never grows an order, and it never silently shrinks one to fit a
risk limit either: an order that breaks a limit is proposed at its real size
and blocked by the risk engine, which says which limit and by how much. (The
per-coin limit is the exception the rule itself honours: it trims a breakout
entry to the room left, as its backtest does, and says so.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import RiskLimits
from .models import PairConstraints, Side
from .numeric import ZERO, format_decimal, quantize_to_increment, round_money


@dataclass(frozen=True)
class SizingResult:
    """A sized order, with the reasoning that produced it."""

    symbol: str
    side: Side
    quantity: Decimal
    notional: Decimal
    reference_price: Decimal
    detail: dict[str, Any] = field(default_factory=dict)
    rejected_reason: str | None = None

    @property
    def viable(self) -> bool:
        return self.rejected_reason is None and self.quantity > ZERO


def size_buy(
    symbol: str,
    *,
    dollars: Decimal,
    reference_price: Decimal,
    constraints: PairConstraints,
    limits: RiskLimits,
) -> SizingResult:
    """Spend ``dollars`` at ``reference_price`` (the ask)."""
    if dollars < limits.min_notional_per_trade_usd:
        return _reject(
            symbol,
            Side.BUY,
            reference_price,
            f"${round_money(dollars)} is below the "
            f"${round_money(limits.min_notional_per_trade_usd)} minimum trade size",
        )
    if reference_price <= ZERO:
        return _reject(symbol, Side.BUY, reference_price, "the reference price is not positive")
    quantity = quantize_to_increment(dollars / reference_price, constraints.quantity_increment)
    return _checked(symbol, Side.BUY, quantity, reference_price, constraints, dollars=dollars)


def size_sell(
    symbol: str,
    *,
    quantity: Decimal,
    reference_price: Decimal,
    constraints: PairConstraints,
) -> SizingResult:
    """Sell ``quantity`` at ``reference_price`` (the bid), snapped down."""
    if reference_price <= ZERO:
        return _reject(symbol, Side.SELL, reference_price, "the reference price is not positive")
    quantity = quantize_to_increment(quantity, constraints.quantity_increment)
    return _checked(symbol, Side.SELL, quantity, reference_price, constraints)


def _checked(
    symbol: str,
    side: Side,
    quantity: Decimal,
    reference_price: Decimal,
    constraints: PairConstraints,
    *,
    dollars: Decimal | None = None,
) -> SizingResult:
    if quantity <= ZERO:
        return _reject(
            symbol,
            side,
            reference_price,
            f"quantity rounds to zero at the pair's {constraints.quantity_increment} increment",
        )
    if constraints.min_order_size is not None and quantity < constraints.min_order_size:
        return _reject(
            symbol,
            side,
            reference_price,
            f"quantity {quantity} is below the pair minimum order size "
            f"{constraints.min_order_size}",
        )
    if constraints.max_order_size is not None and quantity > constraints.max_order_size:
        quantity = quantize_to_increment(
            constraints.max_order_size, constraints.quantity_increment
        )
    notional = round_money(quantity * reference_price)
    if constraints.min_notional is not None and notional < constraints.min_notional:
        return _reject(
            symbol,
            side,
            reference_price,
            f"notional ${notional} is below the pair minimum ${constraints.min_notional}",
        )
    detail: dict[str, Any] = {"quantity_increment": format_decimal(constraints.quantity_increment)}
    if dollars is not None:
        detail["dollars"] = format_decimal(dollars)
    return SizingResult(
        symbol=symbol,
        side=side,
        quantity=quantity,
        notional=notional,
        reference_price=reference_price,
        detail=detail,
    )


def _reject(symbol: str, side: Side, reference_price: Decimal, reason: str) -> SizingResult:
    return SizingResult(
        symbol=symbol,
        side=side,
        quantity=ZERO,
        notional=ZERO,
        reference_price=reference_price,
        rejected_reason=reason,
    )
