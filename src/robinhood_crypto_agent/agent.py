"""The analysis pipeline: market state in, reviewable proposals out.

One pass per watchlist symbol:

    candles -> composite view -> reference price -> size -> plan -> risk -> proposal

Every stage can decline, and a decline is reported rather than swallowed. A
symbol with thin history, a flat signal, or a failing risk rule produces a
:class:`SymbolOutcome` explaining itself -- so "why is there no proposal for
ETH?" always has an answer on the report, instead of an empty list.

Risk-rejected proposals are still constructed and logged. The record of what
the agent wanted to do and was stopped from doing is the evidence that the
risk controls work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from .audit import AuditLog, DailyActivity, proposal_status_for
from .config import AgentConfig
from .execution.kill_switch import KillSwitch
from .execution.orders import STYLE_PROMPT, plan_for_view, snap_price
from .exits import EXIT_TIME, due_lots, open_lots
from .indicators import latest
from .models import (
    CompositeView,
    Direction,
    ExecutionPlan,
    NewsItem,
    OrderType,
    PairConstraints,
    Position,
    Proposal,
    Quote,
    Regime,
    RiskDecision,
    Side,
    Tranche,
    utcnow,
)
from .numeric import ZERO, quantize_to_increment, round_money
from .risk import RiskContext, RiskEngine
from .sizing import SizingResult, size_position
from .store import PriceStore
from .strategy import CompositeStrategy
from .strategy.base import SignalContext
from .symbols import canonical


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
    #: Recent Jev-labeled news; each symbol sees only the items about it.
    news: list[NewsItem] = field(default_factory=list)
    #: When ``positions`` was last ingested. A snapshot newer than a lot's fill
    #: that holds none of the coin means it was sold outside the agent.
    positions_as_of: datetime | None = None

    def quote_for(self, symbol: str) -> Quote | None:
        return self.quotes.get(canonical(symbol))

    def news_for(self, symbol: str) -> list[NewsItem]:
        return [item for item in self.news if item.applies_to(canonical(symbol))]

    def constraints_for(self, symbol: str) -> PairConstraints:
        return self.constraints.get(canonical(symbol)) or PairConstraints.permissive(
            canonical(symbol)
        )


@dataclass
class SymbolOutcome:
    """What the pipeline concluded for one symbol."""

    symbol: str
    view: CompositeView | None
    proposal: Proposal | None
    bars: int
    skipped_reason: str | None = None

    @property
    def produced_proposal(self) -> bool:
        return self.proposal is not None


@dataclass
class AnalysisResult:
    """The outcome of a full analysis run."""

    outcomes: list[SymbolOutcome]
    activity: DailyActivity
    generated_at: object = field(default_factory=utcnow)
    #: Time exits: sells of lots the agent bought and has held the horizon.
    exits: list[Proposal] = field(default_factory=list)

    @property
    def proposals(self) -> list[Proposal]:
        return [o.proposal for o in self.outcomes if o.proposal is not None] + self.exits

    @property
    def executable(self) -> list[Proposal]:
        return [p for p in self.proposals if p.risk.passed]

    @property
    def blocked(self) -> list[Proposal]:
        return [p for p in self.proposals if not p.risk.passed]


class Agent:
    """Runs the analysis pipeline for a configured watchlist."""

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
        self.strategy = CompositeStrategy(config.strategy)
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

        outcomes: list[SymbolOutcome] = []
        for symbol in targets:
            outcome = self._analyze_symbol(symbol, state, activity, kill_state)
            outcomes.append(outcome)
            if record and outcome.proposal is not None:
                self.audit.record_proposal(outcome.proposal)

        exits = [
            p
            for p in self.exit_proposals(state, activity=activity, kill_state=kill_state)
            if p.symbol in targets
        ]
        if record:
            for proposal in exits:
                self.audit.record_proposal(proposal, exit_annotations(proposal))

        return AnalysisResult(outcomes=outcomes, activity=activity, exits=exits)

    def exit_proposals(
        self,
        state: MarketState,
        *,
        activity: DailyActivity | None = None,
        kill_state=None,
        now: datetime | None = None,
    ) -> list[Proposal]:
        """A sell for each symbol with agent lots held ``exit_after_bars`` or longer.

        It sells the lots that are due, capped at what the account holds. When
        the holdings snapshot is older than the lots, a sell is still proposed,
        and ``sell_coverage`` blocks it and says what it saw: re-ingesting
        positions is the fix, and silence would hide the exit.
        """
        bars = self.config.strategy.exit_after_bars
        if bars <= 0:
            return []
        now = now or utcnow()
        hold = timedelta(minutes=bars * self.config.strategy.bar_interval_minutes)
        lots = open_lots(self.audit)
        due = due_lots(lots, now=now, hold=hold)
        if not due:
            return []
        activity = activity or self.audit.daily_activity()
        kill_state = kill_state or self.kill_switch.state()

        proposals = []
        for symbol, ready in sorted(due.items()):
            quote = state.quote_for(symbol)
            if quote is None:
                continue  # the next analysis with a quote proposes it
            held = state.positions.get(symbol)
            held_quantity = held.quantity if held else ZERO
            last_fill = max(lot.filled_at for lot in lots[symbol])
            if held_quantity <= ZERO and state.positions_as_of and state.positions_as_of > last_fill:
                continue  # a snapshot taken after the buy holds none: sold elsewhere
            constraints = state.constraints_for(symbol)
            due_quantity = sum((lot.quantity for lot in ready), ZERO)
            quantity = min(due_quantity, held_quantity) if held_quantity > ZERO else due_quantity
            quantity = quantize_to_increment(quantity, constraints.quantity_increment)
            if quantity <= ZERO:
                continue
            proposals.append(
                self._exit_proposal(
                    symbol, quantity, ready, quote, constraints, state, activity, kill_state,
                    hours=hold.total_seconds() / 3600,
                )
            )
        return proposals

    def _exit_proposal(
        self,
        symbol: str,
        quantity: Decimal,
        lots: list[Any],
        quote: Quote,
        constraints: PairConstraints,
        state: MarketState,
        activity: DailyActivity,
        kill_state,
        *,
        hours: float,
    ) -> Proposal:
        reference_price = quote.bid if quote.bid > ZERO else quote.mark
        since = min(lot.filled_at for lot in lots)
        reason = f"time exit: bought {since:%Y-%m-%d %H:%M} UTC, held past {hours:g}h"
        sizing = SizingResult(
            symbol=symbol,
            side=Side.SELL,
            quantity=quantity,
            notional=round_money(quantity * reference_price),
            reference_price=reference_price,
            detail={
                "exit": EXIT_TIME,
                "entry_proposal_ids": list(dict.fromkeys(lot.proposal_id for lot in lots)),
                "held_since": since.isoformat(),
            },
        )
        view = CompositeView(
            symbol=symbol,
            regime=Regime.UNKNOWN,
            score=0.0,
            confidence=0.0,
            direction=Direction.SHORT,
            signals=[],
            weights={},
            notes=[reason],
        )
        order_type = OrderType.MARKET if constraints.market_orders_only else OrderType.LIMIT
        plan = ExecutionPlan(
            style=STYLE_PROMPT,
            tranches=[
                Tranche(
                    index=0,
                    quantity=quantity,
                    target_price=snap_price(reference_price, Side.SELL, constraints),
                    order_type=order_type,
                )
            ],
            rationale=f"{reason}: sell promptly at the bid, one order",
        )
        context = RiskContext(
            config=self.config,
            quote=quote,
            constraints=constraints,
            activity=activity,
            kill_switch=kill_state,
            positions=state.positions,
            portfolio_value=state.portfolio_value,
            order_type=order_type,
        )
        decision = self.risk_engine.evaluate(view, sizing, context, exit_reason="time exit")
        created_at = utcnow()
        return Proposal(
            proposal_id=Proposal.make_id(symbol, Side.SELL, quantity, created_at),
            symbol=symbol,
            side=Side.SELL,
            quantity=quantity,
            reference_price=reference_price,
            notional=sizing.notional,
            created_at=created_at,
            view=view,
            plan=plan,
            risk=decision,
            status=proposal_status_for(decision.passed),
            sizing_detail=sizing.detail,
            spread_pct=quote.spread_pct,
        )

    def _analyze_symbol(
        self,
        symbol: str,
        state: MarketState,
        activity: DailyActivity,
        kill_state,
    ) -> SymbolOutcome:
        candles = self.store.candles(
            symbol,
            interval_minutes=self.config.strategy.bar_interval_minutes,
            limit=max(self.config.strategy.min_bars * 4, 200),
        )

        if not self.config.allows(symbol):
            return SymbolOutcome(
                symbol=symbol,
                view=None,
                proposal=None,
                bars=len(candles),
                skipped_reason=f"{symbol} is not on the watchlist allowlist",
            )

        quote = state.quote_for(symbol)
        if quote is None:
            return SymbolOutcome(
                symbol=symbol,
                view=None,
                proposal=None,
                bars=len(candles),
                skipped_reason=(
                    f"no live quote for {symbol}; fetch get_crypto_quotes and ingest it "
                    "before analyzing"
                ),
            )

        if not candles:
            return SymbolOutcome(
                symbol=symbol,
                view=None,
                proposal=None,
                bars=0,
                skipped_reason=(
                    f"no price history for {symbol}. The MCP server exposes no crypto "
                    "historicals tool, so history is built by ingesting quotes over "
                    "time (rhca ingest-quotes) or importing bars (rhca import-history)."
                ),
            )

        view = self.strategy.evaluate(symbol, candles, news=state.news_for(symbol))

        if view.direction is Direction.FLAT:
            return SymbolOutcome(
                symbol=symbol,
                view=view,
                proposal=None,
                bars=len(candles),
                skipped_reason=(
                    f"composite score {view.score:+.3f} is inside the neutral band; "
                    "no directional trade"
                ),
            )

        constraints = state.constraints_for(symbol)
        reference_price = self._reference_price(quote, view.direction)

        sizing = size_position(
            view,
            reference_price=reference_price,
            candles=candles,
            limits=self.config.risk,
            strategy=self.config.strategy,
            constraints=constraints,
            portfolio_value=state.portfolio_value,
            position=state.positions.get(canonical(symbol)),
        )

        atr_value = latest(
            SignalContext(
                symbol=symbol, candles=candles, config=self.config.strategy
            ).atr
        )
        plan = plan_for_view(
            sizing, regime=view.regime, constraints=constraints, atr=atr_value
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
        decision: RiskDecision = self.risk_engine.evaluate(view, sizing, context)

        created_at = utcnow()
        proposal = Proposal(
            proposal_id=Proposal.make_id(symbol, sizing.side, sizing.quantity, created_at),
            symbol=symbol,
            side=sizing.side,
            quantity=sizing.quantity,
            reference_price=reference_price,
            notional=sizing.notional,
            created_at=created_at,
            view=view,
            plan=plan,
            risk=decision,
            status=proposal_status_for(decision.passed),
            sizing_detail=sizing.detail,
            spread_pct=quote.spread_pct,
        )

        return SymbolOutcome(
            symbol=symbol, view=view, proposal=proposal, bars=len(candles)
        )

    def _reference_price(self, quote: Quote, direction: Direction) -> Decimal:
        """Price a proposal against the side it would actually cross.

        A buy is referenced to the ask and a sell to the bid, not to the mark.
        Sizing off the mark quietly understates the cost of a wide spread --
        which, on market-maker-routed crypto quotes, is routinely over 1%.
        """
        if direction is Direction.LONG and quote.ask > ZERO:
            return quote.ask
        if direction is Direction.SHORT and quote.bid > ZERO:
            return quote.bid
        return quote.mark


def exit_annotations(proposal: Proposal) -> dict[str, Any]:
    """The audit-log fields that mark a proposal as a time exit.

    ``exit`` keeps it out of the hit rate, and ``trigger_reason`` is what the
    dashboard shows as the reason for it.
    """
    detail = proposal.sizing_detail
    return {
        "exit": detail.get("exit", EXIT_TIME),
        "entry_proposal_ids": detail.get("entry_proposal_ids", []),
        "trigger_reason": proposal.view.notes[0] if proposal.view.notes else "time exit",
        "escalated": False,
    }


def positions_by_symbol(positions: Sequence[Position]) -> dict[str, Position]:
    return {canonical(p.symbol): p for p in positions}


def quotes_by_symbol(quotes: Sequence[Quote]) -> dict[str, Quote]:
    return {canonical(q.symbol): q for q in quotes}


def constraints_by_symbol(
    constraints: Sequence[PairConstraints],
) -> dict[str, PairConstraints]:
    return {canonical(c.symbol): c for c in constraints}


def side_for(direction: Direction) -> Side:
    return Side.BUY if direction is Direction.LONG else Side.SELL
