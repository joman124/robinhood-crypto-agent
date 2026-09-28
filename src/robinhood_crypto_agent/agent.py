"""The analysis pipeline: market state in, reviewable proposals out.

    daily closes + the split's ledger -> strategy.split.decide -> size -> plan -> risk -> proposals

The rule is the split (``strategy/split.py``): half the money bought and held
the buy-low way, half trading the breakout -- the same rules ``rhca backtest
--strategies split`` replays and the forward test keeps on paper. It decides
on the last *closed* UTC day, on Coinbase's daily bars, and each order it
wants becomes one proposal, priced off the live Robinhood quote: the ask for a
buy, the bid for a sell.

Every stage can decline, and a decline is reported rather than swallowed. A
coin with no bars, no quote, or nothing to do today produces a
:class:`SymbolOutcome` explaining itself -- where its stop is, what the
breakout waits for, why a tranche waits -- so "why is there no proposal for
ETH?" always has an answer.

Risk-rejected proposals are still constructed and logged. The record of what
the agent wanted to do and was stopped from doing is the evidence that the
risk controls work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Sequence

from .audit import AuditLog, DailyActivity, proposal_status_for
from .config import AgentConfig
from .daily import DailyBars, latest_close
from .errors import AgentError
from .execution.kill_switch import KillSwitch
from .execution.orders import single_order_plan
from .ledger import RULE_STOP, STRATEGY_SPLIT, SplitBook, split_book
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
from .strategy.breakout import Breakout, day_views
from .strategy.hodl import Accumulate, moving_averages
from .strategy.split import CoinDay, Decision, SplitOrder, decide
from .symbols import canonical

#: How a stop reads in a risk finding that exempts it.
EXIT_REASON = "breakout stop"


def split_rules(config: AgentConfig) -> tuple[Breakout, Accumulate]:
    """The two rules the agent trades, as backtested."""
    return Breakout(), Accumulate(mode=config.strategy.split_long_mode)


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
    #: When ``positions`` was last ingested. A snapshot newer than a sleeve's
    #: last fill that holds none of the coin means it was sold outside the agent.
    positions_as_of: datetime | None = None

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
    proposals: list[Proposal]
    #: Daily bars on hand.
    bars: int
    #: The coin's inputs on the close decided on, when it had them.
    snapshot: CoinDay | None = None
    #: Why there is no proposal: a missing input, or where each sleeve stands.
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
    """Runs the split over the configured watchlist."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        store: PriceStore | None = None,
        audit: AuditLog | None = None,
        kill_switch: KillSwitch | None = None,
        daily: DailyBars | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.store = store or PriceStore(config.price_store_path)
        self.audit = audit or AuditLog(config.audit_path)
        self.kill_switch = kill_switch or KillSwitch(config.kill_switch_path)
        self.daily = daily or DailyBars(config.data_dir / "daily", now=now)
        self.now = now
        self.rule, self.plan = split_rules(config)
        self.risk_engine = RiskEngine(config)

    def book(self) -> SplitBook:
        settings = self.config.strategy
        return split_book(
            self.audit,
            long_capital=settings.split_long_capital,
            short_capital=settings.split_short_capital,
        )

    def analyze(
        self,
        state: MarketState,
        *,
        symbols: Sequence[str] | None = None,
        record: bool = True,
    ) -> AnalysisResult:
        """Decide on the last closed day, for every watchlist coin at once --
        the sleeves share their cash and the per-coin limit -- and propose
        what the rule wants for each coin in ``symbols``."""
        watchlist = [canonical(s) for s in self.config.watchlist]
        targets = [canonical(s) for s in (symbols or watchlist)]
        activity = self.audit.daily_activity()
        kill_state = self.kill_switch.state()
        book = self.book()
        through = latest_close(self.now())

        coins: dict[str, CoinDay] = {}
        bar_counts: dict[str, int] = {}
        problems: dict[str, str] = {}
        for symbol in watchlist:
            try:
                coin, bars = self._coin_day(symbol, state, book, through)
            except AgentError as exc:
                problems[symbol] = f"no daily bars from Coinbase: {exc}"
                continue
            bar_counts[symbol] = bars
            if coin is None:
                problems[symbol] = (
                    f"Coinbase has not published the {through:%Y-%m-%d} daily close yet; "
                    "the split decides on it once it has"
                )
                continue
            coins[symbol] = coin

        decision = decide(
            coins,
            book,
            self.rule,
            self.plan,
            min_trade=self.config.risk.min_notional_per_trade_usd,
            coin_cap_pct=self.config.risk.max_position_pct_of_portfolio,
            account_value=state.portfolio_value,
            long_symbols=watchlist,
        )

        outcomes = [
            self._outcome(symbol, state, book, decision, coins, bar_counts, problems,
                          activity, kill_state)
            for symbol in targets
        ]
        if record:
            for outcome in outcomes:
                for proposal in outcome.proposals:
                    self.audit.record_proposal(proposal, split_annotations(proposal))
        return AnalysisResult(outcomes=outcomes, activity=activity)

    # -- inputs ----------------------------------------------------------------

    def history_days(self, book: SplitBook, through: datetime) -> int:
        """Daily bars to fetch: the slowest average's history, and every day
        since the oldest short-term entry still held."""
        days = max(self.rule.warmup_days, self.plan.average_days) + 3
        for holding in book.short.holdings.values():
            if holding.held and holding.entry_day is not None:
                days = max(days, (through - holding.entry_day).days + 3)
        return days

    def _coin_day(
        self, symbol: str, state: MarketState, book: SplitBook, through: datetime
    ) -> tuple[CoinDay | None, int]:
        bars = [
            b for b in self.daily.get(symbol, days=self.history_days(book, through))
            if b.start <= through
        ]
        if not bars or bars[-1].start < through:
            return None, len(bars)
        last = bars[-1]
        holding = book.short.holdings.get(symbol)
        highest = None
        if holding is not None and holding.held and holding.entry_day is not None:
            since = [b.close for b in bars if holding.entry_day <= b.start <= last.start]
            highest = max(since) if since else None
        quote = state.quote_for(symbol)
        bid = quote.bid if quote is not None and quote.bid > ZERO else last.close
        account = state.positions.get(symbol)
        mark = quote.mark if quote is not None and quote.mark > ZERO else last.close
        coin = CoinDay(
            symbol=symbol,
            day=last.start,
            close=last.close,
            view=day_views(bars, self.rule).get(last.start),
            average=moving_averages(bars, self.plan.average_days).get(last.start),
            highest=highest,
            bid=bid,
            account_holding=account.quantity * mark if account is not None else ZERO,
        )
        return coin, len(bars)

    # -- outputs ---------------------------------------------------------------

    def _outcome(
        self,
        symbol: str,
        state: MarketState,
        book: SplitBook,
        decision: Decision,
        coins: dict[str, CoinDay],
        bar_counts: dict[str, int],
        problems: dict[str, str],
        activity: DailyActivity,
        kill_state: Any,
    ) -> SymbolOutcome:
        bars = bar_counts.get(symbol, 0)
        coin = coins.get(symbol)

        def skip(reason: str) -> SymbolOutcome:
            return SymbolOutcome(symbol, [], bars, coin, reason)

        if not self.config.allows(symbol):
            return skip(f"{symbol} is not on the watchlist allowlist")
        if symbol in problems:
            return skip(problems[symbol])
        quote = state.quote_for(symbol)
        orders = [o for o in decision.orders if o.symbol == symbol]
        if orders and quote is None:
            return skip(
                f"the split wants {len(orders)} order(s) in {symbol}, but there is no live "
                "quote to price them: fetch get_crypto_quotes and ingest it"
            )
        notes = list(decision.notes.get(symbol, []))
        proposals: list[Proposal] = []
        for order in orders:
            assert quote is not None
            proposal, note = self._proposal(order, book, quote, state, activity, kill_state)
            if proposal is not None:
                proposals.append(proposal)
            if note:
                notes.append(note)
        if proposals:
            return SymbolOutcome(symbol, proposals, bars, coin)
        day = f"the {coin.day:%Y-%m-%d} close" if coin else "the last close"
        return skip(f"nothing to do on {day}: " + "; ".join(dict.fromkeys(notes)))

    def _proposal(
        self,
        order: SplitOrder,
        book: SplitBook,
        quote: Quote,
        state: MarketState,
        activity: DailyActivity,
        kill_state: Any,
    ) -> tuple[Proposal | None, str | None]:
        symbol = order.symbol
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
            holding = book.short.holding(symbol)
            account = state.positions.get(symbol)
            account_quantity = account.quantity if account else ZERO
            if (
                account_quantity <= ZERO
                and state.positions_as_of is not None
                and holding.last_fill_at is not None
                and state.positions_as_of > holding.last_fill_at
            ):
                return None, (
                    f"the short-term sleeve's {format_decimal(holding.quantity)} {symbol} is "
                    "not in a holdings snapshot taken after its last fill: it was sold outside "
                    "the agent"
                )
            wanted = order.quantity or holding.quantity
            # Capped at the account's holding. When the snapshot predates the
            # fill it holds none, and sell_coverage blocks the proposal and
            # says so: re-ingesting positions is the fix.
            quantity = min(wanted, account_quantity) if account_quantity > ZERO else wanted
            sizing = size_sell(
                symbol, quantity=quantity, reference_price=reference, constraints=constraints
            )
            if order.rule == RULE_STOP:
                exit_reason = EXIT_REASON

        detail = {
            **sizing.detail,
            STRATEGY_SPLIT: {
                "sleeve": order.sleeve,
                "rule": order.rule,
                "day": order.day.isoformat(),
                **order.detail,
            },
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
                f"{order.sleeve} {order.rule}: one order at the "
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
            reason=order.reason,
            plan=plan,
            risk=decision,
            status=proposal_status_for(decision.passed),
            sizing_detail=sizing.detail,
            spread_pct=quote.spread_pct,
        )
        return proposal, None


def split_annotations(proposal: Proposal) -> dict[str, Any]:
    """The audit-log fields that mark a proposal as the split's.

    ``strategy`` is what the ledger keys on; ``sleeve`` and ``rule`` make the
    log greppable and dedupe the loop's proposals; ``trigger_reason`` is what
    the dashboard shows as the reason for it.
    """
    detail = proposal.sizing_detail.get(STRATEGY_SPLIT) or {}
    return {
        "strategy": STRATEGY_SPLIT,
        "sleeve": detail.get("sleeve"),
        "rule": detail.get("rule"),
        "step": None,
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

