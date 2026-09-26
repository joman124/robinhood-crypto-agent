"""Turning an approved proposal into a validated MCP order payload.

Nothing here places an order. These functions build the *arguments* for
``preview_crypto_order`` and ``place_crypto_order`` and run them through
:func:`~robinhood_crypto_agent.mcp.contract.validate_crypto_order_args`. Claude
is what actually calls the tool, and it does so only after a human has approved
a specific proposal by id.

The execution plan is chosen by regime, because *how* to fill a trade is a
different question from whether to take it:

``PROMPT``
    One order, now, at or near the reference price. A trend signal is
    time-sensitive -- waiting for a better entry in a trending market usually
    means not getting filled at all.

``STAGED``
    Several limit orders laddered away from the mark. In a ranging market
    price is expected to come back, so paying the spread for immediacy is
    wasteful. An unfilled tranche is an acceptable outcome, not a problem to
    fix by chasing.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Sequence

from ..config import AgentConfig
from ..errors import ContractViolation
from ..mcp.contract import CRYPTO_TOOLS, validate_crypto_order_args
from ..models import (
    ExecutionPlan,
    OrderRequest,
    OrderType,
    PairConstraints,
    Proposal,
    Regime,
    Side,
    TimeInForce,
    Tranche,
)
from ..numeric import ZERO, format_decimal, quantize_to_increment
from ..sizing import SizingResult

STYLE_PROMPT = "PROMPT"
STYLE_STAGED = "STAGED"

#: Ladder offsets for a staged entry, as fractions of ATR (or of price when ATR
#: is unavailable). The first tranche sits at the touch so some fill is likely.
STAGED_OFFSETS: tuple[float, ...] = (0.0, 0.5, 1.0)
#: Fallback ladder spacing as a fraction of price, when ATR is unknown.
FALLBACK_OFFSET_PCT = 0.004


def snap_price(price: Decimal, side: Side, constraints: PairConstraints | None) -> Decimal:
    """``price`` on the pair's tick, or unchanged when the tick is unknown.

    Robinhood rejects a limit price off the tick ("round your order price to
    the nearest cent"), and a quote's ask or bid rarely sits on it. A buy limit
    rounds down and a sell limit up, so snapping never makes the order more
    aggressive than it was priced to be.
    """
    increment = constraints.price_increment if constraints is not None else None
    if not increment or increment <= ZERO:
        return price
    rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
    return quantize_to_increment(price, increment, rounding=rounding)


def _limit_price(
    reference: float,
    offset: float,
    side: Side,
    constraints: PairConstraints,
) -> Decimal:
    """Price one rung of a ladder, snapped conservatively to the pair's tick."""
    raw = Decimal(str(reference - offset if side is Side.BUY else reference + offset))
    if raw <= ZERO:
        raw = Decimal(str(reference))
    return snap_price(raw, side, constraints)


def plan_for_view(
    sizing: SizingResult,
    *,
    regime: Regime,
    constraints: PairConstraints,
    atr: float | None = None,
) -> ExecutionPlan:
    """Choose an execution style and lay out its tranches."""
    reference = float(sizing.reference_price)

    if constraints.market_orders_only:
        return ExecutionPlan(
            style=STYLE_PROMPT,
            tranches=[
                Tranche(
                    index=0,
                    quantity=sizing.quantity,
                    target_price=sizing.reference_price,
                    order_type=OrderType.MARKET,
                )
            ],
            rationale=(
                f"{constraints.symbol} accepts market orders only, so the plan is a "
                "single market order regardless of regime"
            ),
        )

    if regime is not Regime.RANGING:
        return ExecutionPlan(
            style=STYLE_PROMPT,
            tranches=[
                Tranche(
                    index=0,
                    quantity=sizing.quantity,
                    target_price=snap_price(sizing.reference_price, sizing.side, constraints),
                    order_type=OrderType.LIMIT,
                )
            ],
            rationale=(
                f"regime is {regime.value}: fill promptly with one marketable limit at "
                "the reference price rather than waiting for a better entry"
            ),
        )

    step = atr if (atr and atr > 0) else reference * FALLBACK_OFFSET_PCT
    tranche_count = len(STAGED_OFFSETS)
    base_quantity = quantize_to_increment(
        sizing.quantity / Decimal(tranche_count), constraints.quantity_increment
    )

    tranches: list[Tranche] = []
    allocated = ZERO
    for index, multiplier in enumerate(STAGED_OFFSETS):
        is_last = index == tranche_count - 1
        # The last tranche absorbs the rounding remainder so the tranches sum
        # to exactly the approved quantity -- never more.
        quantity = (sizing.quantity - allocated) if is_last else base_quantity
        if quantity <= ZERO:
            continue
        allocated += quantity
        tranches.append(
            Tranche(
                index=index,
                quantity=quantity,
                target_price=_limit_price(
                    reference, step * multiplier, sizing.side, constraints
                ),
                order_type=OrderType.LIMIT,
            )
        )

    if not tranches:
        tranches = [
            Tranche(
                index=0,
                quantity=sizing.quantity,
                target_price=snap_price(sizing.reference_price, sizing.side, constraints),
                order_type=OrderType.LIMIT,
            )
        ]

    return ExecutionPlan(
        style=STYLE_STAGED,
        tranches=tranches,
        rationale=(
            f"regime is ranging: ladder {len(tranches)} limit order(s) "
            f"{'below' if sizing.side is Side.BUY else 'above'} the mark, spaced "
            f"{'by ATR' if atr else f'{FALLBACK_OFFSET_PCT:.1%} apart'}. "
            "An unfilled tranche is expected; do not chase it."
        ),
    )


def build_order_request(
    proposal: Proposal,
    *,
    config: AgentConfig,
    tranche_index: int = 0,
    tool: str = CRYPTO_TOOLS["preview"],
    ref_id: str | None = None,
    rhs_account_number: str | None = None,
    constraints: PairConstraints | None = None,
) -> OrderRequest:
    """Build one validated order payload for a proposal's tranche.

    ``constraints`` snaps the limit price to the pair's tick here as well as in
    the plan, so a proposal logged before plans were snapped still produces a
    price Robinhood accepts.

    Raises :class:`ContractViolation` if the resulting payload would be invalid,
    so an unusable order is caught here rather than at Robinhood.
    """
    account = rhs_account_number or config.rhs_account_number
    if not account:
        raise ContractViolation(
            "no rhs_account_number configured. Set it in config/agent.yaml or the "
            "RHCA_RHS_ACCOUNT_NUMBER environment variable -- it is the numeric "
            "'rhs_account_number' from get_accounts, not the alphanumeric "
            "'account_number'."
        )

    tranches = proposal.plan.tranches
    matching = [t for t in tranches if t.index == tranche_index]
    if not matching:
        available = ", ".join(str(t.index) for t in tranches) or "none"
        raise ContractViolation(
            f"proposal {proposal.proposal_id} has no tranche {tranche_index} "
            f"(available: {available})"
        )
    tranche = matching[0]

    arguments: dict[str, object] = {
        "rhs_account_number": str(account),
        "symbol": proposal.symbol,
        "side": proposal.side.value,
        "type": tranche.order_type.value,
        "quantity": format_decimal(tranche.quantity),
    }

    if tranche.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
        arguments["limit_price"] = format_decimal(
            snap_price(tranche.target_price, proposal.side, constraints)
        )
    if tranche.order_type in {OrderType.STOP_LOSS, OrderType.STOP_LIMIT}:
        arguments["stop_price"] = format_decimal(tranche.target_price)

    # market and limit accept only gtc; stops accept the bounded durations.
    arguments["time_in_force"] = TimeInForce.GTC.value

    if tool == CRYPTO_TOOLS["place"]:
        # ref_id is the idempotency key: one per logical order, re-sent verbatim
        # when retrying a transient failure. A preview does not take one.
        arguments["ref_id"] = ref_id or str(uuid.uuid4())

    validate_crypto_order_args(arguments)  # type: ignore[arg-type]

    return OrderRequest(
        tool=tool,
        arguments=arguments,
        proposal_id=proposal.proposal_id,
        tranche_index=tranche_index,
    )


def build_plan_requests(
    proposal: Proposal,
    *,
    config: AgentConfig,
    tool: str = CRYPTO_TOOLS["preview"],
    constraints: PairConstraints | None = None,
) -> list[OrderRequest]:
    """Build a validated payload for every tranche in the proposal's plan."""
    return [
        build_order_request(
            proposal,
            config=config,
            tranche_index=tranche.index,
            tool=tool,
            constraints=constraints,
        )
        for tranche in proposal.plan.tranches
    ]


def describe_requests(requests: Sequence[OrderRequest]) -> str:
    """A human-readable rendering of the tool calls a plan implies."""
    lines: list[str] = []
    for request in requests:
        arguments = ", ".join(f"{k}={v!r}" for k, v in request.arguments.items())
        label = (
            f"tranche {request.tranche_index}"
            if request.tranche_index is not None
            else "order"
        )
        lines.append(f"{label}: {request.tool}({arguments})")
    return "\n".join(lines)
