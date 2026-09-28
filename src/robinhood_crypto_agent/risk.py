"""The risk engine.

Every proposal passes through here before a human is ever shown an option to
approve it. Each rule contributes a :class:`RiskFinding`; a *blocking* failure
means the proposal cannot be executed without an explicit, logged override, and
a non-blocking failure is a warning the report surfaces but that does not stop
the trade.

Two properties matter more than the specific rules:

* **The rules are evaluated for every proposal, and all of them run.** Nothing
  short-circuits on the first failure, so the report shows every reason a trade
  was blocked rather than the first one -- fixing one and rediscovering the
  next is how a limit gets whittled away one edit at a time.
* **The daily caps read from the audit log**, not from memory, so they hold
  across restarts and separate sessions on the same day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .audit import DailyActivity
from .config import AgentConfig
from .execution.kill_switch import KillSwitchState
from .mcp.contract import ORDER_COLLARS
from .models import (
    ExecutionMode,
    OrderType,
    PairConstraints,
    Position,
    Quote,
    RiskDecision,
    RiskFinding,
    Side,
    utcnow,
)
from .numeric import ZERO, round_money
from .sizing import SizingResult
from .symbols import canonical

#: Rules a sell does not answer to, and why. Every sell the split makes is a
#: breakout stop closing a position the sleeve opened, so it only lowers
#: exposure, and blocking it on a cap would leave the position open: the one
#: outcome the stop exists to prevent. Every other rule still applies, the
#: kill switch and sell coverage included.
EXIT_EXEMPT_RULES: dict[str, str] = {
    "per_trade_notional": "the cap limits new exposure; selling what was bought lowers it",
    "daily_notional": "the cap limits new exposure; selling what was bought lowers it",
}


@dataclass
class RiskContext:
    """The live state the risk rules are checked against."""

    config: AgentConfig
    quote: Quote
    constraints: PairConstraints
    activity: DailyActivity
    kill_switch: KillSwitchState
    positions: dict[str, Position] = field(default_factory=dict)
    portfolio_value: Decimal | None = None
    order_type: OrderType = OrderType.LIMIT

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.quantity > ZERO)

    def position_for(self, symbol: str) -> Position | None:
        return self.positions.get(canonical(symbol))


class RiskEngine:
    """Evaluates a sized proposal against every configured limit."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.limits = config.risk

    def evaluate(
        self,
        symbol: str,
        sizing: SizingResult,
        context: RiskContext,
        *,
        exit_reason: str | None = None,
    ) -> RiskDecision:
        """Every rule's finding. ``exit_reason`` names an exit, which passes the
        rules in :data:`EXIT_EXEMPT_RULES` -- each saying so, and what it would
        otherwise have found."""
        findings: list[RiskFinding] = []
        add = findings.append

        add(self._check_execution_mode())
        add(self._check_kill_switch(context))
        add(self._check_watchlist(symbol))
        add(self._check_tradable(context))
        add(self._check_order_type(context))
        add(self._check_quote_freshness(context))
        add(self._check_spread(context))
        add(self._check_sizing(sizing))
        add(self._check_per_trade_notional(sizing))
        add(self._check_daily_notional(sizing, context))
        add(self._check_daily_loss(context))
        add(self._check_open_positions(symbol, sizing, context))
        add(self._check_concentration(sizing, context))
        add(self._check_sell_coverage(sizing, context))

        if exit_reason:
            findings = [_exempted(f, exit_reason) for f in findings]
        return RiskDecision(findings=findings)

    # -- individual rules -------------------------------------------------

    def _check_execution_mode(self) -> RiskFinding:
        mode = self.config.execution_mode
        if mode is ExecutionMode.PROPOSE_ONLY:
            return RiskFinding(
                rule="execution_mode",
                passed=True,
                message="propose-only: a human must approve each proposal by id",
            )
        return RiskFinding(
            rule="execution_mode",
            passed=False,
            message=(
                f"execution_mode is {mode.value!r}. Unattended execution is Phase 2 "
                "and is not implemented yet -- see docs/autonomy.md for what has to "
                "be built and proven first. Until then only 'propose_only' runs."
            ),
        )

    def _check_kill_switch(self, context: RiskContext) -> RiskFinding:
        state = context.kill_switch
        return RiskFinding(
            rule="kill_switch",
            passed=not state.engaged,
            message=state.describe(),
        )

    def _check_watchlist(self, symbol: str) -> RiskFinding:
        allowed = self.config.allows(symbol)
        return RiskFinding(
            rule="watchlist",
            passed=allowed,
            message=(
                f"{symbol} is on the watchlist allowlist"
                if allowed
                else f"{symbol} is not on the watchlist allowlist "
                f"({', '.join(self.config.watchlist)})"
            ),
        )

    def _check_tradable(self, context: RiskContext) -> RiskFinding:
        constraints = context.constraints
        if not constraints.tradable:
            return RiskFinding(
                rule="pair_tradable",
                passed=False,
                message=f"{constraints.symbol} is not tradable on Robinhood",
            )
        if constraints.globally_halted:
            return RiskFinding(
                rule="pair_tradable",
                passed=False,
                message=f"{constraints.symbol} has a global trading halt",
            )
        if constraints.halted:
            regions = ", ".join(constraints.halted_regions) or "unspecified regions"
            return RiskFinding(
                rule="pair_tradable",
                passed=False,
                message=(
                    f"{constraints.symbol} is halted in {regions}. A regional halt may be "
                    "one-sided (e.g. sell-only), so the pair is not assumed orderable."
                ),
                blocking=False,
            )
        return RiskFinding(
            rule="pair_tradable",
            passed=True,
            message=f"{constraints.symbol} is tradable with no active halt",
        )

    def _check_order_type(self, context: RiskContext) -> RiskFinding:
        constraints = context.constraints
        if constraints.supports(context.order_type):
            return RiskFinding(
                rule="order_type_supported",
                passed=True,
                message=f"{constraints.symbol} accepts {context.order_type.value} orders",
            )
        return RiskFinding(
            rule="order_type_supported",
            passed=False,
            message=(
                f"{constraints.symbol} is market-orders-only, so a "
                f"{context.order_type.value} order would be rejected"
            ),
        )

    def _check_quote_freshness(self, context: RiskContext) -> RiskFinding:
        age = (utcnow() - context.quote.observed_at).total_seconds()
        cap = self.limits.max_quote_age_seconds
        return RiskFinding(
            rule="quote_freshness",
            passed=age <= cap,
            message=(
                f"reference quote is {age:.0f}s old (limit {cap}s)"
                + ("" if age <= cap else "; re-fetch the quote before proposing")
            ),
        )

    def _check_spread(self, context: RiskContext) -> RiskFinding:
        spread = context.quote.spread_pct
        cap = self.limits.max_spread_pct
        return RiskFinding(
            rule="spread",
            passed=spread <= cap,
            message=(
                f"bid/ask spread is {spread:.3f}% of mark (limit {cap}%)"
                + (
                    ""
                    if spread <= cap
                    else "; crossing this spread would give up more than the limit allows"
                )
            ),
        )

    def _check_sizing(self, sizing: SizingResult) -> RiskFinding:
        if sizing.rejected_reason:
            return RiskFinding(
                rule="sizing", passed=False, message=sizing.rejected_reason
            )
        return RiskFinding(
            rule="sizing",
            passed=sizing.quantity > ZERO,
            message=(
                f"sized {sizing.quantity} {sizing.symbol} "
                f"(${round_money(sizing.notional)})"
            ),
        )

    def _check_per_trade_notional(self, sizing: SizingResult) -> RiskFinding:
        cap = self.limits.max_notional_per_trade_usd
        # Check the worst case after the market-order collar, not the nominal,
        # so a cap that passes here still holds if the price moves on the way in.
        worst_case = sizing.notional * (Decimal(1) + ORDER_COLLARS[sizing.side])
        return RiskFinding(
            rule="per_trade_notional",
            passed=worst_case <= cap,
            message=(
                f"${round_money(sizing.notional)} notional, worst case "
                f"${round_money(worst_case)} after the {sizing.side.value} collar "
                f"(cap ${cap})"
            ),
        )

    def _check_daily_notional(
        self, sizing: SizingResult, context: RiskContext
    ) -> RiskFinding:
        cap = self.limits.max_daily_notional_usd
        used = context.activity.executed_notional
        projected = used + sizing.notional
        return RiskFinding(
            rule="daily_notional",
            passed=projected <= cap,
            message=(
                f"${round_money(used)} traded today; this trade would bring it to "
                f"${round_money(projected)} (cap ${cap})"
            ),
        )

    def _check_daily_loss(self, context: RiskContext) -> RiskFinding:
        cap = self.limits.max_daily_loss_usd
        loss = context.activity.realized_loss
        return RiskFinding(
            rule="daily_loss",
            passed=loss < cap,
            message=(
                f"realized loss today is ${round_money(loss)} (cap ${cap})"
                + (
                    ""
                    if loss < cap
                    else "; the daily loss cap is reached, so trading stops for the day"
                )
            ),
        )

    def _check_open_positions(
        self, symbol: str, sizing: SizingResult, context: RiskContext
    ) -> RiskFinding:
        cap = self.limits.max_open_positions
        open_now = context.open_position_count
        existing = context.position_for(symbol)
        opens_new = sizing.side is Side.BUY and (existing is None or existing.quantity <= ZERO)
        projected = open_now + (1 if opens_new else 0)
        return RiskFinding(
            rule="open_positions",
            passed=projected <= cap,
            message=(
                f"{open_now} open position(s)"
                + (f", this would open a new one in {symbol}" if opens_new else "")
                + f" (cap {cap})"
            ),
        )

    def _check_concentration(
        self, sizing: SizingResult, context: RiskContext
    ) -> RiskFinding:
        if sizing.side is Side.SELL:
            # Spot-only, so a sell can only shrink the position. Blocking it for
            # leaving the position above the cap would trap exactly the
            # over-concentrated holdings this rule exists to reduce.
            return RiskFinding(
                rule="concentration",
                passed=True,
                message=f"a sell only reduces {sizing.symbol}'s share of the portfolio",
            )
        portfolio = context.portfolio_value
        if portfolio is None or portfolio <= ZERO:
            return RiskFinding(
                rule="concentration",
                passed=True,
                message=(
                    "portfolio value unavailable, so the concentration limit could not "
                    "be checked -- fetch get_portfolio to enforce it"
                ),
                blocking=False,
            )

        existing = context.position_for(sizing.symbol)
        held_value = (
            existing.market_value(context.quote.mark) if existing else ZERO
        )
        projected = (
            held_value + sizing.notional
            if sizing.side is Side.BUY
            else max(ZERO, held_value - sizing.notional)
        )
        pct = projected / portfolio * Decimal(100)
        cap = self.limits.max_position_pct_of_portfolio
        return RiskFinding(
            rule="concentration",
            passed=pct <= cap,
            message=(
                f"{sizing.symbol} would be {pct:.2f}% of the "
                f"${round_money(portfolio)} portfolio (cap {cap}%)"
            ),
        )

    def _check_sell_coverage(
        self, sizing: SizingResult, context: RiskContext
    ) -> RiskFinding:
        if sizing.side is not Side.SELL:
            return RiskFinding(
                rule="sell_coverage",
                passed=True,
                message="buy order; no position coverage required",
            )
        if sizing.rejected_reason:
            # Sizing already refused, so there is no quantity left to cover.
            # Reporting a coverage failure here would name coverage as a
            # blocker on an account holding plenty, and send whoever reads the
            # audit log after the wrong rule. The `sizing` finding carries the
            # real reason; this one has nothing to add.
            return RiskFinding(
                rule="sell_coverage",
                passed=True,
                message=f"not applicable: sizing produced no order ({sizing.rejected_reason})",
            )
        held = context.position_for(sizing.symbol)
        quantity = held.quantity if held else ZERO
        covered = quantity >= sizing.quantity and sizing.quantity > ZERO
        return RiskFinding(
            rule="sell_coverage",
            passed=covered,
            message=(
                f"holding {quantity} {sizing.symbol}, selling {sizing.quantity}"
                + (
                    ""
                    if covered
                    else " -- this agent is spot-only and never sells more than it holds"
                )
            ),
        )


def _exempted(finding: RiskFinding, exit_reason: str) -> RiskFinding:
    why = EXIT_EXEMPT_RULES.get(finding.rule)
    if why is None or finding.passed:
        return finding
    return RiskFinding(
        rule=finding.rule,
        passed=True,
        message=f"not applied to a {exit_reason}: {why} (would have read: {finding.message})",
    )


def summarize(decision: RiskDecision) -> str:
    """A one-line verdict for a report header."""
    if decision.passed:
        warnings = len(decision.warnings)
        suffix = f" ({warnings} warning(s))" if warnings else ""
        return f"PASS{suffix}"
    rules = ", ".join(f.rule for f in decision.blocking_failures)
    return f"BLOCKED by {rules}"
