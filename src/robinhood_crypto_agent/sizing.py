"""Position sizing.

Size is a product of three independent factors, each of which can only ever
*reduce* it:

1. **Conviction** -- ``|score| * confidence``. A half-hearted signal from thin
   data gets a fraction of the budget, not the whole thing.
2. **Volatility scaling** -- when realized volatility runs above the target,
   size is cut proportionally, so a fixed dollar budget does not become a much
   larger risk budget just because the market got twice as violent.
3. **Hard caps** -- the per-trade notional ceiling and the portfolio
   concentration limit.

Because every factor is multiplicative and bounded at 1.0, the result can never
exceed the configured per-trade cap. The resulting quantity is then snapped
*down* to the pair's quantity increment, so the final order is always no larger
than what the caps allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from .config import RiskLimits, StrategyConfig
from .indicators import realized_volatility
from .models import Candle, CompositeView, Direction, PairConstraints, Position, Side
from .numeric import ZERO, format_decimal, format_money, quantize_to_increment, round_money


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


def size_position(
    view: CompositeView,
    *,
    reference_price: Decimal,
    candles: Sequence[Candle],
    limits: RiskLimits,
    strategy: StrategyConfig,
    constraints: PairConstraints,
    portfolio_value: Decimal | None = None,
    position: Position | None = None,
) -> SizingResult:
    """Size a trade for ``view``, or explain why it cannot be sized."""
    side = Side.BUY if view.direction is Direction.LONG else Side.SELL

    def reject(reason: str, **detail: Any) -> SizingResult:
        return SizingResult(
            symbol=view.symbol,
            side=side,
            quantity=ZERO,
            notional=ZERO,
            reference_price=reference_price,
            detail=detail,
            rejected_reason=reason,
        )

    if view.direction is Direction.FLAT:
        return reject("composite view is flat; no directional trade to size")
    if reference_price <= ZERO:
        return reject(f"reference price must be positive, got {reference_price}")

    conviction = abs(view.score) * view.confidence
    if conviction <= 0:
        return reject("conviction is zero (score or confidence is zero)")

    budget = limits.max_notional_per_trade_usd * Decimal(str(round(conviction, 6)))

    vol_scalar = Decimal(1)
    realized = realized_volatility(
        [float(c.close) for c in candles], strategy.volatility_lookback
    )
    if realized is not None and realized > 0:
        realized_pct = Decimal(str(realized * 100))
        if realized_pct > strategy.target_volatility_pct:
            vol_scalar = strategy.target_volatility_pct / realized_pct
        budget *= vol_scalar

    notional = min(budget, limits.max_notional_per_trade_usd)

    concentration_cap: Decimal | None = None
    if portfolio_value is not None and portfolio_value > ZERO:
        concentration_cap = portfolio_value * limits.max_position_pct_of_portfolio / Decimal(100)
        notional = min(notional, concentration_cap)

    if notional < limits.min_notional_per_trade_usd:
        return reject(
            f"sized notional ${round_money(notional)} is below the "
            f"${round_money(limits.min_notional_per_trade_usd)} minimum trade size",
            conviction=conviction,
            volatility_scalar=vol_scalar,
            sized_notional=notional,
        )

    quantity = quantize_to_increment(
        notional / reference_price, constraints.quantity_increment
    )

    if side is Side.SELL:
        held = position.quantity if position else ZERO
        if held <= ZERO:
            return reject(
                "a sell was signalled but no position is held; this agent does not short",
                conviction=conviction,
            )
        if quantity > held:
            # Never sell more than is held -- that would be an accidental short.
            quantity = quantize_to_increment(held, constraints.quantity_increment)

    if quantity <= ZERO:
        return reject(
            f"quantity rounds to zero at the pair's {constraints.quantity_increment} increment",
            sized_notional=notional,
        )

    if constraints.min_order_size is not None and quantity < constraints.min_order_size:
        return reject(
            f"quantity {quantity} is below the pair minimum order size "
            f"{constraints.min_order_size}",
            sized_notional=notional,
        )
    if constraints.max_order_size is not None and quantity > constraints.max_order_size:
        quantity = quantize_to_increment(
            constraints.max_order_size, constraints.quantity_increment
        )

    final_notional = round_money(quantity * reference_price)

    if constraints.min_notional is not None and final_notional < constraints.min_notional:
        return reject(
            f"notional ${final_notional} is below the pair minimum "
            f"${constraints.min_notional}",
            sized_notional=final_notional,
        )

    return SizingResult(
        symbol=view.symbol,
        side=side,
        quantity=quantity,
        notional=final_notional,
        reference_price=reference_price,
        detail={
            "conviction": round(conviction, 6),
            "realized_volatility_pct": (
                float(round(Decimal(str(realized * 100)), 4)) if realized else None
            ),
            "target_volatility_pct": float(strategy.target_volatility_pct),
            "volatility_scalar": float(round(vol_scalar, 6)),
            "budget_before_caps": format_money(budget),
            "concentration_cap": (
                format_money(concentration_cap) if concentration_cap is not None else None
            ),
            "quantity_increment": format_decimal(constraints.quantity_increment),
        },
    )
