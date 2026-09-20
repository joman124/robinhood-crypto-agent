from __future__ import annotations

from robinhood_crypto_agent.models import (
    ExecutionPlan,
    ExecutionStyle,
    ExecutionTranche,
    Regime,
    Side,
    TradeProposal,
)

# Number of resting limit-order tranches for a STAGED plan, weighted toward
# the best (nearest-to-market) price first - mirrors almach's layered
# resting-liquidity technique rather than committing everything at once.
STAGED_TRANCHE_WEIGHTS: tuple[float, ...] = (0.4, 0.35, 0.25)

# Percent distance of each tranche's limit price from the reference price,
# in the favorable direction (below reference for a BUY, above for a SELL).
# The first tranche sits at the reference price itself (fills soonest); each
# later tranche only fills if the market moves further in the favorable
# direction - mirroring almach's pattern of collecting fills across
# different phases of a move rather than all at once.
STAGED_PRICE_STEPS_PCT: tuple[float, ...] = (0.0, 0.35, 0.70)


def build_execution_plan(proposal: TradeProposal) -> ExecutionPlan:
    """Decide how to fill an already-approved proposal, from its regime.

    Purely informational - guides how Claude places MCP orders for a
    proposal a human has already approved. Never authorizes anything on its
    own; execution/adapter.py's propose-only gate is unchanged by this.

    TRENDING (time-sensitive - mo-money's short-timeframe mode pays a
    premium rather than waiting, e.g. ~$1.07 combined cost on 5m markets in
    the source data - waiting isn't worth it when time is short) => PROMPT:
    one order for the full size, at the reference price.

    RANGING (more runway - mo-money's longer-timeframe mode plus almach's
    resting-liquidity technique) => STAGED: several limit-order tranches at
    increasingly favorable prices, filled gradually; an unfilled tranche is
    expected, not a failure - never chase it by crossing the spread, and
    never add size beyond the plan's total.
    """
    if proposal.signal.regime is Regime.TRENDING:
        tranche = ExecutionTranche(
            sequence=0,
            target_price=proposal.reference_price,
            quantity=proposal.quantity,
            notional_usd=proposal.notional_usd,
        )
        return ExecutionPlan(
            style=ExecutionStyle.PROMPT,
            tranches=[tranche],
            rationale=(
                "TRENDING regime: signal is time-sensitive - submit promptly "
                "near the reference price rather than waiting for a better entry."
            ),
        )

    direction = -1.0 if proposal.side is Side.BUY else 1.0
    tranches = [
        ExecutionTranche(
            sequence=sequence,
            target_price=proposal.reference_price * (1 + direction * step_pct / 100),
            quantity=proposal.quantity * weight,
            notional_usd=proposal.notional_usd * weight,
        )
        for sequence, (weight, step_pct) in enumerate(
            zip(STAGED_TRANCHE_WEIGHTS, STAGED_PRICE_STEPS_PCT, strict=True)
        )
    ]

    return ExecutionPlan(
        style=ExecutionStyle.STAGED,
        tranches=tranches,
        rationale=(
            "RANGING regime: signal has more runway - build the position "
            "gradually via resting limit orders at increasingly favorable "
            "prices instead of crossing the spread all at once."
        ),
    )
