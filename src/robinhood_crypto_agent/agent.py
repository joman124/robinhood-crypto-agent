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
from decimal import Decimal
from typing import Sequence

from .audit import AuditLog, DailyActivity, proposal_status_for
from .config import AgentConfig
from .execution.kill_switch import KillSwitch
from .execution.orders import plan_for_view
from .indicators import latest
from .models import (
    CompositeView,
    Direction,
    OrderType,
    PairConstraints,
    Position,
    Proposal,
    Quote,
    RiskDecision,
    Side,
    utcnow,
)
from .numeric import ZERO
from .risk import RiskContext, RiskEngine
from .sizing import size_position
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

    def quote_for(self, symbol: str) -> Quote | None:
        return self.quotes.get(canonical(symbol))

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

    @property
    def proposals(self) -> list[Proposal]:
        return [o.proposal for o in self.outcomes if o.proposal is not None]

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

        return AnalysisResult(outcomes=outcomes, activity=activity)

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

        view = self.strategy.evaluate(symbol, candles)

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
