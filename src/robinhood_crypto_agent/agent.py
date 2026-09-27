"""The analysis pipeline: market state in, reviewable proposals out.

One pass per watchlist symbol:

    closed bars + the ladder's ledger -> Ladder.decide -> size -> plan -> risk -> proposals

The rule is the trend ladder (``strategy.ladder``), the same one ``rhca
backtest`` replays. It decides on the last *closed* bar, as the backtest does,
and each order it wants becomes one proposal, priced off the live quote: the
ask for a buy, the bid for a sell.

Every stage can decline, and a decline is reported rather than swallowed. A
symbol with no history, no quote, or nothing to do this bar produces a
:class:`SymbolOutcome` explaining itself -- including where the next buy and
sell would trigger -- so "why is there no proposal for ETH?" always has an
answer on the report.

Risk-rejected proposals are still constructed and logged. The record of what
the agent wanted to do and was stopped from doing is the evidence that the
risk controls work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from .audit import AuditLog, DailyActivity, proposal_status_for
from .config import AgentConfig
from .execution.kill_switch import KillSwitch
from .execution.orders import single_order_plan
from .ledger import STRATEGY_LADDER, LadderPosition, ladder_positions
from .models import (
    OrderType,
    PairConstraints,
    Position,
    Proposal,
    Quote,
    Side,
    utcnow,
)
from .numeric import ZERO, format_decimal
from .risk import RiskContext, RiskEngine
from .sizing import SizingResult, size_buy, size_sell
from .store import PriceStore
from .strategy.ladder import (
    REASON_DIP,
    REASON_TAKE_PROFIT,
    REASON_TREND_EXIT,
    Order,
    flat_anchor,
    trend_average,
)
from .symbols import canonical

#: How each sell reads in a risk finding that exempts it.
EXIT_REASONS = {REASON_TAKE_PROFIT: "take-profit", REASON_TREND_EXIT: "trend exit"}


@dataclass
class MarketState:
    """The snapshot of Robinhood state an analysis run is evaluated against.

    Assembled by the CLI from MCP tool responses Claude has fetched. Holding it
    as an explicit value -- rather than fetching inside the pipeline -- is what
    makes the whole decision path testable without any network at all.
    """

    quotes: dict[str, Quote] = field(default_factory=dict)
    constraints: dict[str, PairConstraints] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    portfolio_value: Decimal | None = None
    #: When ``positions`` was last ingested. A snapshot newer than the ladder's
    #: last fill that holds none of the coin means it was sold outside the agent.
    positions_as_of: datetime | None = None

    def quote_for(self, symbol: str) -> Quote | None:
        return self.quotes.get(canonical(symbol))

    def constraints_for(self, symbol: str) -> PairConstraints:
        return self.constraints.get(canonical(symbol)) or PairConstraints.permissive(
            canonical(symbol)
        )


@dataclass(frozen=True)
class LadderSnapshot:
    """What the rule saw on the bar it decided on."""

    symbol: str
    bar_start: datetime
    close: Decimal
    anchor: Decimal
    held: Decimal
    in_cycle: bool
    bought: frozenset[int]
    sold: frozenset[int]
    trend_days: int
    #: ``None`` with the filter off, or before the average exists.
    trend_average: Decimal | None
    #: Bars the average needs, and bars on hand.
    trend_bars: int
    bars: int

    @property
    def above(self) -> bool | None:
        return None if self.trend_average is None else self.close > self.trend_average


@dataclass
class SymbolOutcome:
    """What the pipeline concluded for one symbol."""

    symbol: str
    proposals: list[Proposal]
    bars: int
    snapshot: LadderSnapshot | None = None
    #: Why there is no proposal: a missing input, or the ladder's state.
    skipped_reason: str | None = None

    @property
    def produced_proposal(self) -> bool:
        return bool(self.proposals)


@dataclass
class AnalysisResult:
    """The outcome of a full analysis run."""

    outcomes: list[SymbolOutcome]
    activity: DailyActivity
    generated_at: object = field(default_factory=utcnow)

    @property
    def proposals(self) -> list[Proposal]:
        return [p for o in self.outcomes for p in o.proposals]

    @property
    def executable(self) -> list[Proposal]:
        return [p for p in self.proposals if p.risk.passed]

    @property
    def blocked(self) -> list[Proposal]:
        return [p for p in self.proposals if not p.risk.passed]


class Agent:
    """Runs the ladder over a configured watchlist."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        store: PriceStore | None = None,
        audit: AuditLog | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.config = config
        self.store = store or PriceStore(config.price_store_path)
        self.audit = audit or AuditLog(config.audit_path)
        self.kill_switch = kill_switch or KillSwitch(config.kill_switch_path)
        self.ladder = config.strategy.ladder()
        self.risk_engine = RiskEngine(config)

    def analyze(
        self,
        state: MarketState,
        *,
        symbols: Sequence[str] | None = None,
        record: bool = True,
    ) -> AnalysisResult:
        """Evaluate each symbol and produce proposals."""
        targets = [canonical(s) for s in (symbols or self.config.watchlist)]
        activity = self.audit.daily_activity()
        kill_state = self.kill_switch.state()
        positions = ladder_positions(self.audit)

        outcomes: list[SymbolOutcome] = []
        for symbol in targets:
            position = positions.get(symbol) or LadderPosition(symbol)
            outcome = self._analyze_symbol(symbol, state, position, activity, kill_state)
            outcomes.append(outcome)
            if record:
                for proposal in outcome.proposals:
                    self.audit.record_proposal(proposal, ladder_annotations(proposal))

        return AnalysisResult(outcomes=outcomes, activity=activity)

    def _analyze_symbol(
        self,
        symbol: str,
        state: MarketState,
        position: LadderPosition,
        activity: DailyActivity,
        kill_state,
    ) -> SymbolOutcome:
        settings = self.config.strategy
        candles = self.store.candles(symbol, interval_minutes=settings.bar_interval_minutes)

        def skip(reason: str, snapshot: LadderSnapshot | None = None) -> SymbolOutcome:
            return SymbolOutcome(symbol, [], len(candles), snapshot, reason)

        if not self.config.allows(symbol):
            return skip(f"{symbol} is not on the watchlist allowlist")
        quote = state.quote_for(symbol)
        if quote is None:
            return skip(
                f"no live quote for {symbol}; fetch get_crypto_quotes and ingest it "
                "before analyzing"
            )
        if not candles:
            return skip(
                f"no price history for {symbol}. The MCP server has no crypto historicals "
                "tool: `rhca bootstrap-history` imports Coinbase bars, and `rhca run` "
                "records quotes as it goes."
            )

        last = candles[-1]
        average = trend_average(candles, settings.trend_bars) if settings.trend_bars else None
        if position.in_cycle:
            anchor = position.anchor
        else:
            since = _latest(position.flat_since, settings.anchor_since)
            anchor = flat_anchor(candles, since=since)
            if anchor is None:
                return skip(
                    f"no closed bar since {since:%Y-%m-%d %H:%M} UTC, where the anchor "
                    "starts; the first one sets it"
                    if since
                    else "no closed bar yet"
                )
        assert anchor is not None
        snapshot = LadderSnapshot(
            symbol=symbol,
            bar_start=last.start,
            close=last.close,
            anchor=anchor,
            held=position.held,
            in_cycle=position.in_cycle,
            bought=position.bought,
            sold=position.sold,
            trend_days=settings.trend_days,
            trend_average=average,
            trend_bars=settings.trend_bars,
            bars=len(candles),
        )

        orders = self.ladder.decide(
            anchor=anchor,
            close=last.close,
            above=snapshot.above,
            held=position.held,
            bought=position.bought,
            sold=position.sold,
        )
        proposals: list[Proposal] = []
        notes: list[str] = []
        for order in orders:
            if order.side is Side.SELL and position.open_sell:
                notes.append(
                    "a ladder sell order is still open; record its fill or its cancellation "
                    "before another sell is proposed"
                )
                continue
            proposal, note = self._proposal(
                order, snapshot, position, quote, state, activity, kill_state
            )
            if proposal is not None:
                proposals.append(proposal)
            if note:
                notes.append(note)

        if proposals:
            return SymbolOutcome(symbol, proposals, len(candles), snapshot)
        return skip("; ".join([*dict.fromkeys(notes), describe_snapshot(snapshot, self)]), snapshot)

    def _proposal(
        self,
        order: Order,
        snapshot: LadderSnapshot,
        position: LadderPosition,
        quote: Quote,
        state: MarketState,
        activity: DailyActivity,
        kill_state,
    ) -> tuple[Proposal | None, str | None]:
        symbol = snapshot.symbol
        constraints = state.constraints_for(symbol)
        exit_reason = None
        if order.side is Side.BUY:
            assert order.dollars is not None
            reference = quote.ask if quote.ask > ZERO else quote.mark
            sizing = size_buy(
                symbol,
                dollars=order.dollars,
                reference_price=reference,
                constraints=constraints,
                limits=self.config.risk,
            )
        else:
            reference = quote.bid if quote.bid > ZERO else quote.mark
            account = state.positions.get(symbol)
            account_quantity = account.quantity if account else ZERO
            if (
                account_quantity <= ZERO
                and state.positions_as_of is not None
                and position.last_fill_at is not None
                and state.positions_as_of > position.last_fill_at
            ):
                return None, (
                    f"the ladder's {format_decimal(position.held)} {symbol} is not in a "
                    "holdings snapshot taken after its last fill: it was sold outside the agent"
                )
            wanted = position.held
            if not order.everything and order.dollars is not None and reference > ZERO:
                wanted = min(wanted, order.dollars / reference)
            # Capped at the account's holding. When the snapshot predates the
            # fill it holds none, and sell_coverage blocks the proposal and
            # says so: re-ingesting positions is the fix.
            quantity = min(wanted, account_quantity) if account_quantity > ZERO else wanted
            sizing = size_sell(
                symbol, quantity=quantity, reference_price=reference, constraints=constraints
            )
            exit_reason = EXIT_REASONS[order.reason]

        reason = describe_order(order, snapshot, self)
        detail = {
            **sizing.detail,
            STRATEGY_LADDER: ladder_detail(order, snapshot, position, self),
        }
        sizing = SizingResult(
            symbol=sizing.symbol,
            side=sizing.side,
            quantity=sizing.quantity,
            notional=sizing.notional,
            reference_price=sizing.reference_price,
            detail=detail,
            rejected_reason=sizing.rejected_reason,
        )
        plan = single_order_plan(
            sizing,
            constraints=constraints,
            rationale=(
                f"{order.reason.replace('_', ' ')}: one order at the "
                f"{'ask' if order.side is Side.BUY else 'bid'}, as the backtest fills it"
            ),
        )
        context = RiskContext(
            config=self.config,
            quote=quote,
            constraints=constraints,
            activity=activity,
            kill_switch=kill_state,
            positions=state.positions,
            portfolio_value=state.portfolio_value,
            order_type=plan.tranches[0].order_type if plan.tranches else OrderType.LIMIT,
        )
        decision = self.risk_engine.evaluate(symbol, sizing, context, exit_reason=exit_reason)
        created_at = utcnow()
        proposal = Proposal(
            proposal_id=Proposal.make_id(symbol, sizing.side, sizing.quantity, created_at),
            symbol=symbol,
            side=sizing.side,
            quantity=sizing.quantity,
            reference_price=sizing.reference_price,
            notional=sizing.notional,
            created_at=created_at,
            reason=reason,
            plan=plan,
            risk=decision,
            status=proposal_status_for(decision.passed),
            sizing_detail=sizing.detail,
            spread_pct=quote.spread_pct,
        )
        return proposal, None


def _latest(*moments: datetime | None) -> datetime | None:
    present = [m for m in moments if m is not None]
    return max(present) if present else None


def _price(value: Decimal) -> str:
    return f"{value:,.2f}" if abs(value) >= 1 else format_decimal(value)


def _pct_from(value: Decimal, anchor: Decimal) -> Decimal:
    return (value / anchor - Decimal(1)) * Decimal(100)


def _trend_phrase(snapshot: LadderSnapshot) -> str:
    if not snapshot.trend_days:
        return "no trend filter"
    if snapshot.trend_average is None:
        return (
            f"its {snapshot.trend_days}-day average needs {snapshot.trend_bars} bars and "
            f"{snapshot.bars} are on hand, so nothing is bought yet (rhca bootstrap-history)"
        )
    side = "above" if snapshot.above else "at or under"
    return f"{side} its {snapshot.trend_days}-day average {_price(snapshot.trend_average)}"


def describe_order(order: Order, snapshot: LadderSnapshot, agent: Agent) -> str:
    """One line: which rule fired, on what numbers."""
    ladder = agent.ladder
    close, anchor = snapshot.close, snapshot.anchor
    if order.reason == REASON_TREND_EXIT:
        return (
            f"trend exit: closed {_price(close)}, {_trend_phrase(snapshot)}; "
            f"sell everything the ladder holds"
        )
    assert order.step is not None
    pct, dollars = ladder.steps[order.step]
    if order.reason == REASON_DIP:
        return (
            f"dip: closed {_price(close)}, {-_pct_from(close, anchor):.1f}% under the anchor "
            f"{_price(anchor)}; step {order.step + 1} buys ${format_decimal(dollars)} at -{pct}%"
            f" ({_trend_phrase(snapshot)})"
        )
    what = (
        "sells everything left"
        if order.everything
        else f"sells ${format_decimal(dollars)} worth"
    )
    return (
        f"take profit: closed {_price(close)}, {_pct_from(close, anchor):.1f}% over the "
        f"anchor {_price(anchor)}; step {order.step + 1} {what} at +{pct}%"
    )


def describe_snapshot(snapshot: LadderSnapshot, agent: Agent) -> str:
    """Nothing to do this bar: where the ladder stands, and what would move it."""
    ladder = agent.ladder
    close, anchor = snapshot.close, snapshot.anchor
    parts = [
        f"closed {_price(close)} ({_pct_from(close, anchor):+.1f}% from the anchor "
        f"{_price(anchor)})"
    ]
    if snapshot.held > ZERO:
        steps = ", ".join(str(s + 1) for s in sorted(snapshot.bought)) or "none"
        parts.append(f"holding {format_decimal(snapshot.held)} (steps bought: {steps})")
    else:
        parts.append("holding none")
    next_buy = next(
        (i for i in range(len(ladder.steps)) if i not in snapshot.bought), None
    )
    if next_buy is not None:
        parts.append(
            f"step {next_buy + 1} buys at {_price(ladder.buy_level(anchor, next_buy))}"
        )
    if snapshot.held > ZERO:
        next_sell = next(
            (i for i in range(len(ladder.steps)) if i not in snapshot.sold), None
        )
        if next_sell is not None:
            parts.append(
                f"step {next_sell + 1} sells at {_price(ladder.sell_level(anchor, next_sell))}"
            )
    parts.append(_trend_phrase(snapshot))
    return "no order this bar: " + "; ".join(parts)


def ladder_detail(
    order: Order, snapshot: LadderSnapshot, position: LadderPosition, agent: Agent
) -> dict[str, Any]:
    """The rule's state, stored on the proposal. The ledger reads ``step`` and
    ``anchor`` back to rebuild the cycle once the order fills."""
    ladder = agent.ladder
    level = None
    if order.step is not None:
        level = (
            ladder.buy_level(snapshot.anchor, order.step)
            if order.side is Side.BUY
            else ladder.sell_level(snapshot.anchor, order.step)
        )
    return {
        "rule": order.reason,
        "step": order.step,
        "anchor": format_decimal(snapshot.anchor),
        "close": format_decimal(snapshot.close),
        "bar": snapshot.bar_start.isoformat(),
        "level": format_decimal(level) if level is not None else None,
        "dollars": format_decimal(order.dollars) if order.dollars is not None else None,
        "everything": order.everything,
        "trend_days": snapshot.trend_days,
        "trend_average": (
            format_decimal(snapshot.trend_average) if snapshot.trend_average is not None else None
        ),
        "held": format_decimal(position.held),
        "entry_proposal_ids": position.entry_proposal_ids if order.side is Side.SELL else [],
    }


def ladder_annotations(proposal: Proposal) -> dict[str, Any]:
    """The audit-log fields that mark a proposal as the ladder's.

    ``strategy`` is what the ledger keys on, ``rule`` and ``step`` make the
    log greppable, and ``trigger_reason`` is what the dashboard shows as the
    reason for it.
    """
    detail = proposal.sizing_detail.get(STRATEGY_LADDER) or {}
    return {
        "strategy": STRATEGY_LADDER,
        "rule": detail.get("rule"),
        "step": detail.get("step"),
        "trigger_reason": proposal.reason,
    }


def positions_by_symbol(positions: Sequence[Position]) -> dict[str, Position]:
    return {canonical(p.symbol): p for p in positions}


def quotes_by_symbol(quotes: Sequence[Quote]) -> dict[str, Quote]:
    return {canonical(q.symbol): q for q in quotes}


def constraints_by_symbol(
    constraints: Sequence[PairConstraints],
) -> dict[str, PairConstraints]:
    return {canonical(c.symbol): c for c in constraints}
