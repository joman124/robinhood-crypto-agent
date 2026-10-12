"""The forward test: the split account on paper, one UTC daily close at a time.

``rhca backtest --strategies split`` asks how the split *would have* done.
This asks how it *does*: the same two rules, from a start date fixed before
any price it trades existed (``config/shadow.yaml``), on Coinbase's public
daily closes -- one request per coin per day.

After each UTC daily close, ``rhca run`` replays the paper account from the
start date and writes one ``shadow_day`` record to the audit log: what the
paper account did on that close, and Robinhood's bid and ask for every coin it
traded, read at that moment. ``rhca shadow`` replays it again and reports,
against the bar in docs/strategy.md ("The forward test").

Replaying from the start each day, rather than carrying state forward, keeps
the paper account identical to what the backtest code computes for the same
days: there is one implementation of each rule, and no state to drift.

Each paper fill is also put through the risk engine as if it were a proposal,
against the paper account, so the record says whether a human *could* have
approved it. ``rhca shadow`` then prices the fills that passed at the ask and
bid Robinhood showed: what the split would have made had each been approved.

**Nothing here trades.** A shadow day is not a proposal: it carries no
proposal id, ``rhca approve`` cannot find it, and no code path leads from it
to an order. A risk verdict on a paper fill is a record, not an approval. The paper account exists to learn, before any money is at stake,
whether the split behaves live as it did in its backtest, at Robinhood's real
spread.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from .agent import EXIT_REASON
from .audit import KIND_SHADOW_DAY, AuditLog, DailyActivity
from .backtest import DEFAULT_ROUND_TRIP_PCT, money, pct
from .bootstrap import fetch_coinbase_history
from .config import AgentConfig, ShadowConfig
from .daily import latest_close
from .errors import AgentError
from .execution.kill_switch import KillSwitch, KillSwitchState
from .models import (
    Candle,
    PairConstraints,
    Position,
    Quote,
    RiskDecision,
    parse_timestamp,
    utcnow,
)
from .numeric import ZERO, format_decimal, round_money, to_decimal
from .portfolio_backtest import (
    PortfolioResult,
    Prepared,
    SplitResult,
    Trade,
    run_equal_hold,
    run_split,
)
from .risk import RiskContext, RiskEngine, summarize
from .sizing import size_buy, size_sell
from .store import StateCache
from .strategy.breakout import Breakout, day_views
from .strategy.hodl import Accumulate

log = logging.getLogger("rhca.shadow")

DAY = timedelta(days=1)
DAILY_MINUTES = 24 * 60
LONG_TERM = "long-term"
SHORT_TERM = "short-term"

#: How often ``rhca run`` looks for a new daily close. A look that finds none
#: costs nothing: the fetch happens once per closed day.
CHECK_INTERVAL_SECONDS = 600
#: How long after a close to wait for a coin Coinbase has not published yet,
#: before recording the day without it.
LATE_GRACE = timedelta(hours=6)

#: The bar, fixed 2026-09-28 before any forward close existed (docs/strategy.md).
BAR_DAYS = 90
#: The breakout's worst 90-day window in the 4-year backtest: its return, and
#: its max drawdown, in percent.
WORST_WINDOW_RETURN_PCT = Decimal("-5.7")
WORST_WINDOW_DRAWDOWN_PCT = Decimal("10.1")


def rules(config: AgentConfig) -> tuple[Breakout, Accumulate]:
    """The two rules, as ``rhca backtest`` runs them by default."""
    shadow = _shadow(config)
    return Breakout(), Accumulate(mode=shadow.long_mode)


def coin_cap_pct(config: AgentConfig) -> Decimal:
    """The most of the whole paper account one coin may be: the live limit."""
    return config.risk.max_position_pct_of_portfolio


def long_tranches(config: AgentConfig) -> int:
    """How many tranches each long-term coin's share is bought in."""
    shadow = _shadow(config)
    _, plan = rules(config)
    coins = len(shadow.long_symbols or shadow.symbols)
    return plan.tranches_for(shadow.long_capital / coins, config.risk.min_notional_per_trade_usd)


def _shadow(config: AgentConfig) -> ShadowConfig:
    if config.shadow is None:
        raise AgentError(
            "no forward test is configured: config/shadow.yaml is missing "
            "(docs/strategy.md, 'The forward test')"
        )
    return config.shadow


def setup(config: AgentConfig) -> dict[str, Any]:
    """What the test is, stamped on every record: records made under another
    setup belong to another test, and are left out of this one."""
    shadow = _shadow(config)
    return {
        "start": shadow.start.date().isoformat(),
        "capital": str(shadow.capital),
        "long_pct": str(shadow.long_pct),
        "long_mode": shadow.long_mode,
        "symbols": list(shadow.symbols),
        "long_symbols": list(shadow.long_symbols) if shadow.long_symbols else None,
        "coin_cap_pct": str(coin_cap_pct(config)),
    }


def history_days(config: AgentConfig, *, now: datetime) -> int:
    """Daily bars to fetch: every day since the start, and the warm-up the
    slowest average needs before it."""
    shadow = _shadow(config)
    rule, plan = rules(config)
    since = max(0, (now - shadow.start).days)
    return since + max(rule.warmup_days, plan.average_days) + 3


Fetch = Callable[..., list[Candle]]


def fetch_daily(
    symbols: Iterable[str], *, days: int, fetch: Fetch | None = None
) -> dict[str, list[Candle]]:
    """Closed UTC daily bars per coin, oldest first (Coinbase's, by default)."""
    fetch = fetch or fetch_coinbase_history
    return {s: fetch(s, interval_minutes=DAILY_MINUTES, days=days) for s in symbols}


@dataclass(frozen=True)
class PaperFill:
    """One thing the paper account did on one daily close."""

    day: datetime
    sleeve: str
    symbol: str
    side: str
    #: Dollars paid (a buy) or received (a sell), spread included.
    dollars: Decimal
    quantity: Decimal
    #: The daily close it filled against, before the spread.
    close: Decimal

    @property
    def key(self) -> str:
        return f"{self.sleeve}|{self.symbol}|{self.side}"


@dataclass
class Replay:
    """The paper account from the start date through ``through``."""

    through: datetime
    split: SplitResult
    hold: PortfolioResult
    prepared: dict[str, Prepared]
    rule: Breakout
    fills: list[PaperFill]

    @property
    def closes(self) -> int:
        """Daily closes counted so far."""
        return len(self.split.combined.equity_curve)

    def fills_on(self, day: datetime) -> list[PaperFill]:
        return [f for f in self.fills if f.day == day]


def replay(
    daily: Mapping[str, Sequence[Candle]], config: AgentConfig, *, through: datetime
) -> Replay:
    """The split, and hold for comparison, from the start through ``through``."""
    shadow = _shadow(config)
    rule, plan = rules(config)
    prepared = {s: Prepared(list(bars), day_views(bars, rule)) for s, bars in daily.items() if bars}
    window = dict(start=shadow.start, end=through + DAY)
    split = run_split(
        prepared,
        rule,
        plan,
        capital=shadow.capital,
        long_pct=shadow.long_pct,
        round_trip_pct=DEFAULT_ROUND_TRIP_PCT,
        min_trade=config.risk.min_notional_per_trade_usd,
        long_symbols=shadow.long_symbols,
        coin_cap_pct=coin_cap_pct(config),
        **window,
    )
    hold = run_equal_hold(
        prepared, capital=shadow.capital, round_trip_pct=DEFAULT_ROUND_TRIP_PCT, **window
    )
    closes = {s: {bar.start: bar.close for bar in p.daily} for s, p in prepared.items()}
    return Replay(through, split, hold, prepared, rule, paper_fills(split, closes))


def paper_fills(
    split: SplitResult, closes: Mapping[str, Mapping[datetime, Decimal]]
) -> list[PaperFill]:
    fills: list[PaperFill] = []
    for trade in split.short.trades:
        fills.append(
            PaperFill(trade.entry_day, SHORT_TERM, trade.symbol, "buy", trade.cost,
                      trade.quantity, closes[trade.symbol][trade.entry_day])
        )
        if trade.exit_day is not None:
            fills.append(
                PaperFill(trade.exit_day, SHORT_TERM, trade.symbol, "sell", trade.proceeds,
                          trade.quantity, closes[trade.symbol][trade.exit_day])
            )
    for trade in split.long.trades:
        fills.append(
            PaperFill(trade.entry_day, LONG_TERM, trade.symbol, "buy", trade.cost,
                      trade.quantity, closes[trade.symbol][trade.entry_day])
        )
    return sorted(fills, key=lambda f: (f.day, f.sleeve, f.symbol, f.side))


def spread_pct(quote: Quote) -> Decimal | None:
    mid = (quote.bid + quote.ask) / 2
    return (quote.ask - quote.bid) / mid * 100 if mid > ZERO else None


def risk_checks(
    result: Replay,
    day: datetime,
    quotes: Mapping[str, Quote],
    pairs: Mapping[str, PairConstraints],
    kill_state: KillSwitchState,
    config: AgentConfig,
) -> dict[str, RiskDecision]:
    """Every risk rule's verdict on each of ``day``'s paper fills, by fill key.

    Each fill goes through the engine a proposal goes through, priced off
    Robinhood's quote, against the paper account: what it held that morning,
    its value at the last close, and the fills before this one that day. A
    fill with no quote gets no verdict. A verdict is a record, not an approval.
    """
    engine = RiskEngine(config)
    split = result.split
    held: dict[str, Decimal] = {}
    for trade in split.short.trades + split.long.trades:
        if trade.entry_day < day and (trade.exit_day is None or trade.exit_day >= day):
            held[trade.symbol] = held.get(trade.symbol, ZERO) + trade.quantity
    before = [equity for when, equity in split.combined.equity_curve if when < day]
    value = before[-1] if before else split.combined.capital
    notional, realized, count = ZERO, ZERO, 0
    verdicts: dict[str, RiskDecision] = {}
    for fill in result.fills_on(day):
        quote = quotes.get(fill.symbol)
        if quote is None:
            continue
        buy = fill.side == "buy"
        constraints = pairs.get(fill.symbol) or PairConstraints.permissive(fill.symbol)
        sizing = (
            size_buy(fill.symbol, dollars=fill.dollars, reference_price=quote.ask,
                     constraints=constraints, limits=config.risk)
            if buy
            else size_sell(fill.symbol, quantity=fill.quantity, reference_price=quote.bid,
                           constraints=constraints)
        )
        activity = DailyActivity(
            day=day.date(), executed_notional=notional, execution_count=count,
            realized_pnl=realized, proposal_count=0, symbols_traded=(),
        )
        context = RiskContext(
            config=config, quote=quote, constraints=constraints, activity=activity,
            kill_switch=kill_state, positions={s: Position(s, q) for s, q in held.items()},
            portfolio_value=value,
        )
        verdicts[fill.key] = engine.evaluate(
            fill.symbol, sizing, context, exit_reason=None if buy else EXIT_REASON
        )
        notional += fill.dollars
        count += 1
        held[fill.symbol] = held.get(fill.symbol, ZERO) + (fill.quantity if buy else -fill.quantity)
        if not buy:
            realized += sum(
                (t.pnl for t in split.short.trades if t.symbol == fill.symbol and t.exit_day == day),
                ZERO,
            )
    return verdicts


def _verdict(decision: RiskDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "passed": decision.passed,
        "summary": summarize(decision),
        "findings": [f.to_dict() for f in decision.findings],
    }


def _account(result: PortfolioResult) -> dict[str, str]:
    return {
        "capital": str(round_money(result.capital)),
        "equity": str(round_money(result.final_equity)),
    }


def day_record(
    result: Replay,
    day: datetime,
    quotes: Mapping[str, Quote],
    config: AgentConfig,
    verdicts: Mapping[str, RiskDecision] | None = None,
) -> dict[str, Any]:
    """The ``shadow_day`` audit record for one close."""
    verdicts = verdicts or {}
    shadow_fills = []
    for fill in result.fills_on(day):
        quote = quotes.get(fill.symbol)
        spread = spread_pct(quote) if quote is not None else None
        shadow_fills.append(
            {
                "sleeve": fill.sleeve,
                "symbol": fill.symbol,
                "side": fill.side,
                "dollars": str(round_money(fill.dollars)),
                "quantity": format_decimal(fill.quantity),
                "close": format_decimal(fill.close),
                "bid": format_decimal(quote.bid) if quote else None,
                "ask": format_decimal(quote.ask) if quote else None,
                "spread_pct": format_decimal(spread.quantize(Decimal("0.0001"))) if spread else None,
                "risk": _verdict(verdicts.get(fill.key)),
            }
        )
    split = result.split
    return {
        "day": day.date().isoformat(),
        "mode": "paper",
        "setup": setup(config),
        "fills": shadow_fills,
        "accounts": {
            LONG_TERM: _account(split.long),
            SHORT_TERM: _account(split.short),
            "split": _account(split.combined),
            "hold": _account(result.hold),
        },
    }


def snapshot(result: Replay, config: AgentConfig) -> dict[str, Any]:
    """What ``rhca status`` shows, written after each recorded day."""
    shadow = _shadow(config)
    split = result.split
    return {
        "updated_at": utcnow().isoformat(),
        "setup": setup(config),
        "start": shadow.start.date().isoformat(),
        "through": result.through.date().isoformat(),
        "closes": result.closes,
        "accounts": {
            LONG_TERM: _account(split.long),
            SHORT_TERM: _account(split.short),
            "split": _account(split.combined),
            "hold": _account(result.hold),
        },
        "open": sorted({f"{t.symbol}" for t in split.short.trades if t.open}),
    }


class ShadowTracker:
    """What ``rhca run`` calls on its forward-test cadence."""

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLog,
        robinhood: Any,
        *,
        fetch: Fetch | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        _shadow(config)
        self.config = config
        self.audit = audit
        self.robinhood = robinhood
        self._fetch = fetch
        self._now = now
        self._recorded = set(logged_days(audit, config))

    def update(self) -> Replay | None:
        """Record the latest closed day, once. ``None`` when there is nothing new."""
        shadow = _shadow(self.config)
        now = self._now()
        through = latest_close(now)
        if through < shadow.start or through.date().isoformat() in self._recorded:
            return None
        daily = fetch_daily(
            shadow.symbols, days=history_days(self.config, now=now), fetch=self._fetch
        )
        late = sorted(s for s, bars in daily.items() if not bars or bars[-1].start < through)
        if late and now < through + DAY + LATE_GRACE:
            raise AgentError(
                f"Coinbase has not published the {through:%Y-%m-%d} daily close for "
                f"{', '.join(late)} yet; trying again shortly"
            )
        result = replay(daily, self.config, through=through)
        today = result.fills_on(through)
        quotes = self._quotes({f.symbol for f in today})
        verdicts = (
            risk_checks(
                result, through, quotes,
                StateCache(self.config.data_dir / "market_state.json").pairs(),
                KillSwitch(self.config.kill_switch_path).state(),
                self.config,
            )
            if quotes
            else {}
        )
        record = day_record(result, through, quotes, self.config, verdicts)
        if late:
            record["missing"] = late
        self.audit.append(KIND_SHADOW_DAY, record)
        self._recorded.add(record["day"])
        return result

    def _quotes(self, symbols: set[str]) -> dict[str, Quote]:
        """Robinhood's bid and ask now, for the coins traded on paper today.
        Only a record of the real spread: a failure costs the spread, not the day."""
        if not symbols:
            return {}
        try:
            quotes = self.robinhood.best_bid_ask(sorted(symbols))
        except AgentError as exc:
            log.warning("forward test: could not read Robinhood quotes: %s", exc)
            return {}
        return {q.symbol: q for q in quotes if q.symbol in symbols}


# -- the report --------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    #: ``None`` while there is not yet anything to judge.
    passed: bool | None
    detail: str


def logged_days(audit: AuditLog, config: AgentConfig) -> dict[str, dict[str, Any]]:
    """This test's ``shadow_day`` records, by day (the last wins)."""
    current = setup(config)
    return {
        str(r.get("day")): r
        for r in audit.events(kind=KIND_SHADOW_DAY)
        if r.get("setup") == current
    }


def _logged_fill(
    logged: Mapping[str, Mapping[str, Any]], day: datetime, key: str
) -> Mapping[str, Any] | None:
    """The fill ``rhca run`` recorded under ``key`` on ``day``: ``None`` when it
    did not record the day at all, ``{}`` when the day has no such fill."""
    record = logged.get(day.date().isoformat())
    if record is None:
        return None
    return next(
        (
            f for f in record.get("fills") or []
            if f"{f.get('sleeve')}|{f.get('symbol')}|{f.get('side')}" == key
        ),
        {},
    )


@dataclass(frozen=True)
class WhatIf:
    """One paper trade, had a human approved its entry whenever it passed risk."""

    sleeve: str
    trade: Trade
    #: The entry's risk verdict, or why there is none.
    note: str
    #: Its worth (or what it sold for) at Robinhood's prices; ``None`` when not taken.
    worth: Decimal | None = None
    #: Blocked by the risk check, as against never checked.
    blocked: bool = False


def what_if(result: Replay, logged: Mapping[str, Mapping[str, Any]]) -> list[WhatIf]:
    """Every paper trade, taken at Robinhood's prices where its entry passed risk.

    In: the paper dollars, at the ask ``rhca run`` read that close. Out: the
    bid it read on the close the trade sold. A trade still open -- or sold on a
    close ``rhca run`` did not record -- is valued as the paper account values
    it: the close, less half the round trip.
    """
    trades = [(SHORT_TERM, t) for t in result.split.short.trades]
    trades += [(LONG_TERM, t) for t in result.split.long.trades]
    rows: list[WhatIf] = []
    for sleeve, trade in sorted(trades, key=lambda st: (st[1].entry_day, st[0], st[1].symbol)):
        entry = _logged_fill(logged, trade.entry_day, f"{sleeve}|{trade.symbol}|buy")
        risk = (entry or {}).get("risk")
        if entry is None:
            rows.append(WhatIf(sleeve, trade, "not checked: rhca run did not record this close"))
            continue
        if not risk or not entry.get("ask"):
            rows.append(WhatIf(sleeve, trade, "not checked: no Robinhood quote or verdict"))
            continue
        if not risk.get("passed"):
            rows.append(WhatIf(sleeve, trade, str(risk.get("summary") or "BLOCKED"), blocked=True))
            continue
        quantity = trade.cost / to_decimal(entry["ask"], field="ask")
        # ponytail: the stop is counted as sold even if its own verdict was a
        # block (only the kill switch or a spread over the limit can block one).
        # Ceiling: a blocked stop held on would be worth more or less than this.
        exit_fill = (
            _logged_fill(logged, trade.exit_day, f"{sleeve}|{trade.symbol}|sell")
            if trade.exit_day is not None
            else None
        )
        price = (
            to_decimal(exit_fill["bid"], field="bid")
            if exit_fill and exit_fill.get("bid")
            else trade.proceeds / trade.quantity
        )
        rows.append(WhatIf(sleeve, trade, "passes risk", worth=quantity * price))
    return rows


def render_what_if(rows: Sequence[WhatIf]) -> list[str]:
    """``rhca shadow``'s section on taking every paper trade that passed risk."""
    lines = [
        "",
        "  Had every paper trade that passed risk been approved, at Robinhood's prices",
        "  (hypothetical: none of these is a proposal, and none can be approved)",
    ]
    if not rows:
        return lines + ["  no paper trades yet"]
    for row in rows:
        trade = row.trade
        if row.worth is None:
            outcome = "not taken"
        else:
            state = "open" if trade.open else f"sold {trade.exit_day:%Y-%m-%d}"
            outcome = f"{state}, {money(row.worth)} ({money(row.worth - trade.cost)})"
        lines.append(
            f"  {trade.entry_day:%Y-%m-%d} {row.sleeve:<11} {trade.symbol:<9}"
            f"{money(trade.cost):>9} in  {row.note}; {outcome}"
        )
    taken = [r for r in rows if r.worth is not None]
    blocked = sum(1 for r in rows if r.blocked)
    unchecked = len(rows) - len(taken) - blocked
    if taken:
        cost = sum((r.trade.cost for r in taken), ZERO)
        worth = sum((r.worth for r in taken if r.worth is not None), ZERO)
        paper = sum((r.trade.pnl for r in taken), ZERO)
        lines.append(
            f"  taken: {len(taken)} trade(s), {money(cost)} in, worth {money(worth)} now: "
            f"{money(worth - cost)} ({pct((worth / cost - 1) * 100)}); "
            f"the paper account made {money(paper)} on the same trades"
        )
    lines.append(f"  not taken: {blocked} blocked by risk, {unchecked} not checked")
    return lines


def check_bar(
    result: Replay, window: Replay, logged: Mapping[str, Mapping[str, Any]]
) -> list[Check]:
    """The forward test's three checks. ``window`` is the replay cut at the
    bar's last day, or the whole replay until then."""
    checks: list[Check] = []

    mismatched = []
    compared = 0
    for day_text, record in sorted(logged.items()):
        day = datetime.fromisoformat(day_text).replace(tzinfo=timezone.utc)
        if day > result.through:
            continue
        compared += 1
        logged_keys = sorted(
            f"{f.get('sleeve')}|{f.get('symbol')}|{f.get('side')}" for f in record.get("fills") or []
        )
        if logged_keys != sorted(f.key for f in result.fills_on(day)):
            mismatched.append(day_text)
    checks.append(
        Check(
            "Faithful: every day rhca run logged matches this replay",
            None if compared == 0 else not mismatched,
            (
                "no day logged yet"
                if compared == 0
                else f"{compared - len(mismatched)} of {compared} logged days match"
                + (f"; differs on {', '.join(mismatched)}" if mismatched else "")
            ),
        )
    )

    spreads = [
        to_decimal(f["spread_pct"], field="spread_pct")
        for record in logged.values()
        for f in record.get("fills") or []
        if f.get("spread_pct") is not None
    ]
    median = statistics.median(spreads) if spreads else None
    checks.append(
        Check(
            f"Costs: median Robinhood spread at fill time <= {DEFAULT_ROUND_TRIP_PCT}%",
            None if median is None else median <= DEFAULT_ROUND_TRIP_PCT,
            (
                "no paper fill with a Robinhood quote yet"
                if median is None
                else f"{median:.2f}% over {len(spreads)} fill(s)"
            ),
        )
    )

    short = window.split.short
    in_range = (
        short.total_return_pct >= WORST_WINDOW_RETURN_PCT
        and short.max_drawdown_pct <= WORST_WINDOW_DRAWDOWN_PCT
    )
    checks.append(
        Check(
            f"In range: the breakout sleeve returns >= {WORST_WINDOW_RETURN_PCT}% with a max "
            f"drawdown <= {WORST_WINDOW_DRAWDOWN_PCT}%",
            in_range,
            f"{pct(short.total_return_pct)}, drawdown {pct(-short.max_drawdown_pct)}",
        )
    )
    return checks


def _row(name: str, result: PortfolioResult) -> str:
    return (
        f"  {name:<30}{money(result.capital):>10}{money(result.final_equity):>11}"
        f"{pct(result.total_return_pct):>9}{pct(-result.max_drawdown_pct):>9}"
        f"{float(result.final_exposure_pct):>9.0f}%"
    )


def render(
    result: Replay, window: Replay, config: AgentConfig, logged: Mapping[str, Mapping[str, Any]]
) -> str:
    shadow = _shadow(config)
    split = result.split
    bar_day = shadow.start + DAY * (BAR_DAYS - 1)
    lines = [
        "=" * 110,
        "FORWARD TEST: THE SPLIT ON PAPER",
        "=" * 110,
        "Paper only: nothing here is a proposal or an order. docs/strategy.md, 'The forward test'.",
        f"Counting from the {shadow.start:%Y-%m-%d} daily close: {result.closes} close(s) so far, "
        f"through {result.through:%Y-%m-%d}. The bar is read after {BAR_DAYS} closes, on "
        f"{bar_day + DAY:%Y-%m-%d}.",
        f"{money(split.long.capital)} long-term "
        f"({Accumulate(mode=shadow.long_mode).describe(long_tranches(config))})",
        f"{money(split.short.capital)} short-term in the breakout; every fill crosses half the "
        f"{DEFAULT_ROUND_TRIP_PCT}% round trip. No coin may be more than "
        f"{coin_cap_pct(config)}% of the account.",
        "",
        f"  {'account':<30}{'capital':>10}{'equity':>11}{'return':>9}{'max dd':>9}{'in coins':>10}",
        _row(split.long.name, split.long),
        _row(split.short.name, split.short),
        _row(split.combined.name, split.combined),
        _row(result.hold.name, result.hold),
    ]

    open_short = [t for t in split.short.trades if t.open]
    held_long: dict[str, list[Any]] = {}
    for trade in split.long.trades:
        held_long.setdefault(trade.symbol, []).append(trade)
    if open_short or held_long:
        lines += ["", "  held now"]
    for trade in open_short:
        prepared = result.prepared[trade.symbol]
        closes = [b.close for b in prepared.daily if trade.entry_day <= b.start <= result.through]
        view = prepared.views.get(result.through)
        stop = (
            f", stop {format_decimal(round_money(result.rule.stop(max(closes), view)))}"
            if view is not None and closes
            else ""
        )
        lines.append(
            f"  {SHORT_TERM:<11} {trade.symbol:<9} bought {trade.entry_day:%Y-%m-%d} for "
            f"{money(trade.cost)}, worth {money(trade.proceeds)} at the bid{stop}"
        )
    for symbol, trades in sorted(held_long.items()):
        cost = sum((t.cost for t in trades), ZERO)
        worth = sum((t.proceeds for t in trades), ZERO)
        lines.append(
            f"  {LONG_TERM:<11} {symbol:<9} {len(trades)} tranche(s) for {money(cost)}, "
            f"worth {money(worth)} at the bid"
        )

    if result.fills:
        lines += ["", f"  paper fills ({len(result.fills)})"]
    for fill in result.fills:
        match = _logged_fill(logged, fill.day, fill.key)
        if match is None:
            note = "not logged live: rhca run did not record this day"
        elif not match:
            note = "NOT in rhca run's record for this day"
        else:
            spread = match.get("spread_pct")
            note = f"logged live, Robinhood spread {spread}%" if spread else "logged live"
            if match.get("risk"):
                note += f", risk {match['risk'].get('summary')}"
        lines.append(
            f"  {fill.day:%Y-%m-%d} {fill.sleeve:<11} {fill.side:<5}{fill.symbol:<9}"
            f"{money(fill.dollars):>10} at the {format_decimal(fill.close)} close  ({note})"
        )

    lines += render_what_if(what_if(result, logged))

    checks = check_bar(result, window, logged)
    done = result.closes >= BAR_DAYS
    lines += [
        "",
        f"  The bar (fixed 2026-09-28), read after {BAR_DAYS} closes"
        + ("" if done else f" -- {result.closes} so far, so every verdict is provisional") + ":",
    ]
    for number, check in enumerate(checks, start=1):
        verdict = "-" if check.passed is None else ("pass" if check.passed else "FAIL")
        if not done and check.passed is not None:
            verdict += " so far"
        lines.append(f"  {number}. {check.name}")
        lines.append(f"     {check.detail}  [{verdict}]")
    if done:
        lines += ["", conclusion(checks)]
    return "\n".join(lines)


def conclusion(checks: Sequence[Check]) -> str:
    failed = [c.name.split(":")[0] for c in checks if c.passed is False]
    unknown = [c.name.split(":")[0] for c in checks if c.passed is None]
    if failed:
        return (
            f"It fails {', '.join(failed)}: the split did not behave live as in its backtest. "
            "Do not trade it; find out why first."
        )
    if unknown:
        return f"Not yet judged: nothing to measure {', '.join(unknown)} by. Keep it running."
    return (
        "All three pass: the split behaved live as in its backtest. Trading it with real "
        "money is still the owner's decision, and needs the risk-limit changes in "
        "docs/strategy.md ('The split')."
    )


def report(
    config: AgentConfig, audit: AuditLog, *, fetch: Fetch | None = None, now: datetime | None = None
) -> str:
    """``rhca shadow``: fetch, replay, and render."""
    shadow = _shadow(config)
    now = now or utcnow()
    through = latest_close(now)
    if through < shadow.start:
        return (
            f"The forward test counts from the {shadow.start:%Y-%m-%d} daily close; there is "
            f"nothing to report until {shadow.start + DAY:%Y-%m-%d} 00:00 UTC."
        )
    daily = fetch_daily(shadow.symbols, days=history_days(config, now=now), fetch=fetch)
    missing = sorted(s for s, bars in daily.items() if not bars)
    if missing:
        raise AgentError(f"Coinbase returned no daily bars for {', '.join(missing)}")
    # Report through the last close every coin has.
    through = min(through, *(bars[-1].start for bars in daily.values()))
    result = replay(daily, config, through=through)
    bar_last = shadow.start + DAY * (BAR_DAYS - 1)
    window = result if through <= bar_last else replay(daily, config, through=bar_last)
    return render(result, window, config, logged_days(audit, config))


def describe_status(config: AgentConfig, payload: Mapping[str, Any] | None) -> list[str]:
    """``rhca status`` lines, from the snapshot the last recorded day wrote."""
    if config.shadow is None:
        return ["forward test   : off (no config/shadow.yaml)"]
    if not payload or payload.get("setup") != setup(config):
        return [
            f"forward test   : counting from the {config.shadow.start:%Y-%m-%d} close; nothing "
            "recorded yet -- `rhca run` records each UTC daily close (paper only)"
        ]
    accounts = payload.get("accounts") or {}

    def account(name: str) -> str:
        entry = accounts.get(name) or {}
        try:
            capital = to_decimal(entry["capital"], field="capital")
            equity = to_decimal(entry["equity"], field="equity")
        except (KeyError, AgentError):
            return f"{name} ?"
        change = (equity / capital - 1) * 100 if capital > ZERO else ZERO
        return f"{name} {money(equity)} ({pct(change)})"

    updated = payload.get("updated_at")
    try:
        age = f", updated {parse_timestamp(str(updated)):%Y-%m-%d %H:%M} UTC" if updated else ""
    except ValueError:
        age = ""
    held = ", ".join(payload.get("open") or []) or "nothing"
    return [
        f"forward test   : {payload.get('closes', '?')} close(s) since {payload.get('start')}, "
        f"through {payload.get('through')}{age} (paper only; `rhca shadow` for the report)",
        f"  {account('split')} = {account(LONG_TERM)} + {account(SHORT_TERM)}; "
        f"{account('hold')}; breakout holds {held}",
    ]
