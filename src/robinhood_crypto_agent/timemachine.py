"""The Time Machine: a split trade's odds, replayed on its coin's own past days.

Borrowed from day-trader-skill's ``trader-timemachine`` (MIT), and adapted to
a rule that decides on daily closes and pays a 1.9% round trip. It answers two
questions (docs/timemachine.md):

- **A breakout entry** (``rhca timemachine SOL-USD``, or ``--proposal ID``):
  if the breakout buys this coin at this close, how often does its trailing
  stop sell it within 30, 60 and 90 days, and what is the trade worth, in R,
  when the stop or the last day comes? Beside the replays stands **the
  history**: the same rule on the coin's real closes over the same days.
- **The forward test** (``--forward-test``): how likely is it to reach the
  three priced paper fills its bar needs (docs/forward-test-exit-criteria.md)
  by its first read, and by its one extension?

**How a replay is made.** Each real day becomes four ratios to the close
before it: open, high, low and close. A replay strings real days together in
blocks of consecutive days -- each day after the first starts a new block with
chance 1 / ``block_days`` -- and scales each one to the replay's own last
close. Blocks keep calm and wild stretches together, as crypto's are; in the
forward test every coin draws the *same* past days, which keeps how they move
together. The draws come from ``random.Random(seed)``: the same bars, seed and
settings give the same numbers on any machine.

**The rule replayed is the rule traded.** The replayed bars go through the
live rule's own code: ``day_views`` and ``Breakout.exits`` for a trade, and
``shadow.run_paper`` -- what ``rhca shadow`` runs -- for the forward test. R
is the backtest's: in at the close plus half the round trip, out at a close
less half, over what the initial stop would have lost.

**What it is not.** The replays are made of the coin's own past, so they carry
its past drift, and they know nothing about whether today is a breakout: they
are odds, not a forecast. Nothing here proposes, approves or orders, and
nothing here writes to the audit log.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from . import shadow as shadow_mod
from .agent import split_rules
from .audit import AuditLog
from .backtest import DEFAULT_ROUND_TRIP_PCT
from .config import AgentConfig
from .daily import latest_close
from .errors import AgentError
from .ledger import RULE_ENTRY, RULE_STOP, RULE_TRANCHE, split_info
from .models import Candle
from .numeric import ZERO, format_decimal
from .portfolio_backtest import Prepared, Trade, run_breakout
from .strategy.breakout import Breakout, DayView, day_views

DAY = timedelta(days=1)
ONE_HUNDRED = Decimal("100")

DEFAULT_REPLAYS = 2000
DEFAULT_SEED = 1
#: The mean length of a block of consecutive past days, in days.
DEFAULT_BLOCK_DAYS = 10
#: How far back the replays draw from, and the history reads: the four years
#: the breakout's backtest was judged on.
DEFAULT_SAMPLE_DAYS = 1460
#: How long a replayed trade runs before it is marked, in days.
DEFAULT_HORIZON = 90
#: The odds are printed every this many days of the horizon.
CHECKPOINT_DAYS = 30
#: The paper fills, with a Robinhood quote, the forward test's bar needs to
#: mean anything (docs/forward-test-exit-criteria.md, section 2).
FILLS_NEEDED = 3
#: Its one extension, in daily closes from the start (same doc, section 3).
EXTENSION_CLOSES = 180

#: Daily bars of one coin, at least ``days`` of them, oldest first.
Bars = Callable[[str, int], list[Candle]]

#: What a paper fill was, by its sleeve and side.
ENTRY, STOP, TRANCHE = "entry", "stop", "tranche"
_KINDS = {
    (shadow_mod.SHORT_TERM, "buy"): ENTRY,
    (shadow_mod.SHORT_TERM, "sell"): STOP,
    (shadow_mod.LONG_TERM, "buy"): TRANCHE,
}


# -- replaying past days --------------------------------------------------


@dataclass(frozen=True)
class Move:
    """One real day, as ratios to the close before it."""

    day: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def moves(daily: Sequence[Candle]) -> dict[datetime, Move]:
    """Each day that follows the day before it, as a :class:`Move`."""
    out: dict[datetime, Move] = {}
    for previous, bar in zip(daily, daily[1:], strict=False):
        base = previous.close
        if bar.start - previous.start != DAY or base <= ZERO:
            continue
        out[bar.start] = Move(bar.start, bar.open / base, bar.high / base, bar.low / base,
                              bar.close / base)
    return out


class Sampler:
    """Strings past days into replays, in blocks of consecutive days.

    Every coin draws the same days, so a replay keeps how they moved
    together. Only days every coin has can be drawn."""

    def __init__(
        self,
        history: Mapping[str, Mapping[datetime, Move]],
        *,
        block_days: int = DEFAULT_BLOCK_DAYS,
        seed: int = DEFAULT_SEED,
    ) -> None:
        if block_days < 1:
            raise AgentError("--block-days must be at least 1")
        days = set.intersection(*(set(m) for m in history.values())) if history else set()
        if not days:
            raise AgentError("there is no past day with every coin's bar to replay")
        self.history = history
        self.days = sorted(days)
        self.block_days = block_days
        self.rng = random.Random(seed)

    def draw(self, count: int) -> list[datetime]:
        """The past days a replay of ``count`` days is made of, in order."""
        rng, days = self.rng, self.days
        index = rng.randrange(len(days))
        drawn: list[datetime] = []
        for step in range(count):
            if step:
                follows = index + 1 < len(days) and days[index + 1] - days[index] == DAY
                if follows and rng.random() >= 1 / self.block_days:
                    index += 1
                else:
                    index = rng.randrange(len(days))
            drawn.append(days[index])
        return drawn

    def bars(self, last: Candle, drawn: Sequence[datetime]) -> list[Candle]:
        """The days after ``last``, each a drawn day's moves scaled to the close
        before it."""
        out: list[Candle] = []
        close, day = last.close, last.start
        for past in drawn:
            move = self.history[last.symbol][past]
            day += DAY
            out.append(
                Candle(last.symbol, day, day + DAY, close * move.open, close * move.high,
                       close * move.low, close * move.close, 0)
            )
            close = out[-1].close
        return out


def future_views(
    real: Sequence[Candle], future: Sequence[Candle], rule: Breakout
) -> dict[datetime, DayView]:
    """``day_views`` of the real bars and ``future`` after them, for the future
    days only -- computed on just the real tail the windows reach back into."""
    tail = list(real[-(rule.warmup_days + 1):]) + list(future)
    views = day_views(tail, rule)
    return {bar.start: views[bar.start] for bar in future if bar.start in views}


def _window(daily: Sequence[Candle], through: datetime, days: int) -> list[Candle]:
    """The last ``days`` bars through ``through``."""
    return [b for b in daily if through - DAY * (days - 1) <= b.start <= through]


def _quantile(values: Sequence[Any], q: float) -> Any:
    """The nearest-rank ``q`` quantile of sorted ``values``."""
    return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))]


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _rate(value: float) -> str:
    return f"{value * 100:.1f} %"


def _p(value: Decimal) -> str:
    return f"{value:,.2f}" if abs(value) >= 1 else format_decimal(value.normalize())


def _r(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:+.2f}R"


# -- a breakout entry -----------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A breakout entry on one daily close: the trade the replays run."""

    symbol: str
    day: datetime
    view: DayView
    #: The rule buys on this close: a close over both lines.
    breakout: bool
    #: The initial stop: the chandelier's, with the entry close the highest.
    stop: Decimal
    #: The close the breakout buys over: the higher of the prior high and the
    #: average.
    line: Decimal
    proposal_id: str | None = None


@dataclass(frozen=True)
class Outcome:
    """One replayed trade."""

    #: Days after the entry the stop sold it; ``None`` when still held at the end.
    stopped_on: int | None
    r: Decimal
    #: The replay's close on its last day, whether or not the stop sold.
    last_close: Decimal


@dataclass
class TradeOdds:
    entry: Entry
    rule: Breakout
    replays: int
    seed: int
    block_days: int
    horizon: int
    round_trip_pct: Decimal
    #: The first and last real day the replays drew from, and how far the coin
    #: moved over them.
    sampled: tuple[datetime, datetime]
    sampled_move_pct: Decimal
    outcomes: list[Outcome] = field(default_factory=list)
    #: The same rule on the coin's real closes over the sampled days.
    history: list[Trade] = field(default_factory=list)

    def checkpoints(self) -> list[int]:
        steps = list(range(CHECKPOINT_DAYS, self.horizon, CHECKPOINT_DAYS))
        return [*steps, self.horizon]

    def stopped_by(self, days: int, *, gain: bool | None = None) -> float:
        hits = [
            o for o in self.outcomes
            if o.stopped_on is not None and o.stopped_on <= days
            and (gain is None or (o.r > ZERO) == gain)
        ]
        return _share(len(hits), len(self.outcomes))

    @property
    def held(self) -> float:
        return _share(sum(o.stopped_on is None for o in self.outcomes), len(self.outcomes))

    @property
    def rs(self) -> list[Decimal]:
        return sorted(o.r for o in self.outcomes)

    @property
    def mean_r(self) -> Decimal:
        rs = self.rs
        return sum(rs, ZERO) / Decimal(len(rs)) if rs else ZERO

    @property
    def positive(self) -> float:
        return _share(sum(o.r > ZERO for o in self.outcomes), len(self.outcomes))

    @property
    def closed_history(self) -> list[Trade]:
        return [t for t in self.history if not t.open]

    @property
    def history_mean_r(self) -> Decimal | None:
        rs = [t.r for t in self.closed_history if t.r is not None]
        return sum(rs, ZERO) / Decimal(len(rs)) if rs else None


def entry_on(
    symbol: str, daily: Sequence[Candle], day: datetime, rule: Breakout,
    *, proposal_id: str | None = None,
) -> Entry:
    """The breakout entry at ``day``'s close, from its real bars."""
    view = day_views(daily, rule).get(day)
    if view is None:
        raise AgentError(
            f"{symbol} has no breakout indicators for the {day:%Y-%m-%d} close: it needs "
            f"{rule.warmup_days + 1} daily closes up to it"
        )
    if view.atr <= ZERO:
        raise AgentError(f"{symbol}'s ATR on the {day:%Y-%m-%d} close is zero: no stop to replay")
    return Entry(
        symbol, day, view, rule.enters(view), rule.stop(view.close, view),
        max(view.prior_high, view.average), proposal_id,
    )


def proposal_entry(audit: AuditLog, proposal_id: str) -> tuple[str, datetime]:
    """The coin and the close a split breakout-entry proposal was decided on."""
    record = audit.find_proposal(proposal_id)
    if record is None:
        raise AgentError(f"no proposal {proposal_id} in the audit log")
    info = split_info(record)
    if info is None or info.rule != RULE_ENTRY:
        what = {
            RULE_TRANCHE: "a long-term tranche, which is held for good: there is no stop to replay",
            RULE_STOP: "a breakout stop, a sell: there is no trade left to replay",
        }.get(info.rule if info else "", "not a split breakout entry")
        raise AgentError(f"{proposal_id} is {what}")
    return info.symbol, info.day


def replay_trade(
    daily: Sequence[Candle],
    entry: Entry,
    rule: Breakout,
    *,
    replays: int = DEFAULT_REPLAYS,
    seed: int = DEFAULT_SEED,
    block_days: int = DEFAULT_BLOCK_DAYS,
    sample_days: int = DEFAULT_SAMPLE_DAYS,
    horizon: int = DEFAULT_HORIZON,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
) -> TradeOdds:
    """``replays`` futures of ``horizon`` days after the entry, each run
    through the breakout's chandelier stop; and the rule's real history over
    the days drawn from."""
    if replays < 1 or horizon < 1 or sample_days < 2:
        raise AgentError("--replays and --horizon must be at least 1, --sample-days at least 2")
    if not ZERO <= round_trip_pct < ONE_HUNDRED:
        raise AgentError("--round-trip must be a percent from 0 up to 100")
    real = [b for b in daily if b.start <= entry.day]
    sample = _window(real, entry.day, sample_days)
    drawn_from = moves(sample)
    sampler = Sampler({entry.symbol: drawn_from}, block_days=block_days, seed=seed)
    half = round_trip_pct / Decimal(200)
    quantity = 1 / (entry.view.close * (1 + half))
    risk = rule.stop_fraction(entry.view)

    def trade(exit_day: datetime | None, close: Decimal) -> Trade:
        # The backtest's Trade, one dollar in: R does not depend on the size.
        return Trade(entry.symbol, entry.day, exit_day, quantity, Decimal(1),
                     quantity * close * (1 - half), risk)

    odds = TradeOdds(
        entry, rule, replays, seed, block_days, horizon, round_trip_pct,
        sampled=(min(drawn_from), max(drawn_from)),
        sampled_move_pct=(sample[-1].close / sample[0].close - 1) * ONE_HUNDRED,
    )
    for _ in range(replays):
        future = sampler.bars(real[-1], sampler.draw(horizon))
        views = future_views(real, future, rule)
        highest, stopped_on, close, exit_day = entry.view.close, None, future[-1].close, None
        for index, bar in enumerate(future, 1):
            view = views[bar.start]
            highest = max(highest, view.close)
            if rule.exits(highest, view):
                stopped_on, close, exit_day = index, view.close, bar.start
                break
        r = trade(exit_day, close).r
        odds.outcomes.append(Outcome(stopped_on, r if r is not None else ZERO, future[-1].close))

    prepared = {entry.symbol: Prepared(real, day_views(real, rule))}
    odds.history = run_breakout(
        prepared, rule, round_trip_pct=round_trip_pct, start=sample[0].start, end=entry.day
    ).trades
    return odds


def trade_odds(
    config: AgentConfig,
    audit: AuditLog,
    bars: Bars,
    *,
    now: datetime,
    symbol: str | None = None,
    proposal_id: str | None = None,
    sample_days: int = DEFAULT_SAMPLE_DAYS,
    **settings: Any,
) -> TradeOdds:
    """``rhca timemachine COIN`` (an entry at the last close) or ``--proposal ID``
    (the close a breakout-entry proposal was decided on)."""
    rule, _ = split_rules(config)
    if proposal_id:
        symbol, day = proposal_entry(audit, proposal_id)
    elif symbol:
        day = latest_close(now)
    else:
        raise AgentError("name a coin, a breakout-entry --proposal, or --forward-test")
    back = max(0, (latest_close(now) - day).days)
    daily = [b for b in bars(symbol, back + sample_days + rule.warmup_days + 2) if b.start <= day]
    if not daily:
        raise AgentError(f"Coinbase returned no daily bars for {symbol}")
    if proposal_id is None:
        day = daily[-1].start
    entry = entry_on(symbol, daily, day, rule, proposal_id=proposal_id)
    return replay_trade(daily, entry, rule, sample_days=sample_days, **settings)


def trade_verdict(odds: TradeOdds) -> list[str]:
    """One line on the replays' mean R, and the lines that qualify it."""
    mean = odds.mean_r
    size = abs(mean)
    lean = "positive" if mean > ZERO else "negative"
    if size < Decimal("0.1"):
        head = f"Close to break-even after costs: mean R {_r(mean)}."
    elif size < Decimal("0.3"):
        head = f"Leans {lean}: mean R {_r(mean)}."
    else:
        head = f"Strongly {lean}: mean R {_r(mean)}."
    lines = [head]
    median = _quantile(odds.rs, 0.5)
    if size >= Decimal("0.1") and (median > ZERO) != (mean > ZERO):
        lines.append(
            f"The typical replay went the other way (median {_r(median)}): a few large "
            "replays carry the mean."
        )
    history = odds.history_mean_r
    if history is not None and (history > ZERO) != (mean > ZERO):
        lines.append(
            f"The replays and the history disagree: the rule's closed trades on "
            f"{odds.entry.symbol} averaged {_r(history)}."
        )
    if len(odds.closed_history) < 5:
        lines.append(
            f"The history is thin: {len(odds.closed_history)} closed trade(s) to judge it by."
        )
    return lines


def render_trade(odds: TradeOdds) -> str:
    entry, rule = odds.entry, odds.rule
    gap = (entry.view.close - entry.stop) / entry.view.close * ONE_HUNDRED
    source = f"proposal {entry.proposal_id}" if entry.proposal_id else "the last close"
    lines = [
        f"Time Machine: a breakout entry on {entry.symbol} at the {entry.day:%Y-%m-%d} close, "
        f"{_p(entry.view.close)} ({source})",
        f"  stop {_p(entry.stop)} today, {gap:.1f}% under: {rule.stop_atr} x ATR({rule.atr_days}) "
        f"{_p(entry.view.atr)} under the highest close, trailing it up",
    ]
    if not entry.breakout:
        lines.append(
            f"  not a breakout today: the rule buys a close over {_p(entry.line)} (the higher "
            f"of its prior {rule.breakout_days}-day high and {rule.regime_days}-day average)"
        )
    first, last = odds.sampled
    lines += [
        "",
        f"{odds.replays:,} replays of the next {odds.horizon} days, drawn in blocks (mean "
        f"{odds.block_days} days) from {entry.symbol}'s days {first:%Y-%m-%d} to "
        f"{last:%Y-%m-%d}, seed {odds.seed}.",
        f"Over those days {entry.symbol} moved {odds.sampled_move_pct:+.0f}%; the replays "
        "carry that drift. R after a "
        f"{format_decimal(odds.round_trip_pct)}% round trip.",
    ]
    for days in odds.checkpoints():
        lines.append(
            f"  {f'stopped out by day {days}':<24}{_rate(odds.stopped_by(days)):>7}   "
            f"(at a gain {_rate(odds.stopped_by(days, gain=True))}, "
            f"at a loss {_rate(odds.stopped_by(days, gain=False))})"
        )
    lines.append(f"  {f'still held at day {odds.horizon}':<24}{_rate(odds.held):>7}")
    rs = odds.rs
    lines += [
        f"  R at the stop or day {odds.horizon}: mean {_r(odds.mean_r)}, median "
        f"{_r(_quantile(rs, 0.5))}, 90% between {_r(_quantile(rs, 0.05))} and "
        f"{_r(_quantile(rs, 0.95))}; above zero in {_rate(odds.positive)}",
    ]
    closes = sorted(o.last_close for o in odds.outcomes)
    lines.append(
        f"  Price on day {odds.horizon}: median {_p(_quantile(closes, 0.5))}, 90% of replays "
        f"between {_p(_quantile(closes, 0.05))} and {_p(_quantile(closes, 0.95))}."
    )
    closed = odds.closed_history
    held_days = [(t.exit_day - t.entry_day).days for t in closed if t.exit_day]
    won = sum(1 for t in closed if t.r is not None and t.r > ZERO)
    open_now = len(odds.history) - len(closed)
    lines += [
        "",
        f"History: the same rule on {entry.symbol}'s real closes over those days entered "
        f"{len(odds.history)} time(s): {len(closed)} closed ({won} won, {len(closed) - won} "
        f"lost){f', {open_now} still held' if open_now else ''}."
        + (
            f" Mean R {_r(odds.history_mean_r)} on the closed, held a median "
            f"{statistics.median(held_days):.0f} days."
            if held_days
            else ""
        ),
        "",
        *trade_verdict(odds),
        "Odds from the coin's own past days, not a forecast; they know nothing of today's "
        "news. Nothing here is a proposal or an approval.",
    ]
    return "\n".join(lines)


def trade_summary(odds: TradeOdds) -> dict[str, Any]:
    entry, rs = odds.entry, odds.rs
    closed = odds.closed_history
    return {
        "symbol": entry.symbol,
        "day": entry.day.date().isoformat(),
        "proposal_id": entry.proposal_id,
        "close": format_decimal(entry.view.close),
        "stop": format_decimal(entry.stop),
        "breakout": entry.breakout,
        "replays": odds.replays,
        "seed": odds.seed,
        "block_days": odds.block_days,
        "horizon_days": odds.horizon,
        "round_trip_pct": format_decimal(odds.round_trip_pct),
        "sampled": [odds.sampled[0].date().isoformat(), odds.sampled[1].date().isoformat()],
        "sampled_move_pct": f"{odds.sampled_move_pct:.2f}",
        "stopped_by": {str(d): round(odds.stopped_by(d), 4) for d in odds.checkpoints()},
        "held": round(odds.held, 4),
        "r": {
            "mean": f"{odds.mean_r:.4f}",
            "median": f"{_quantile(rs, 0.5):.4f}",
            "p05": f"{_quantile(rs, 0.05):.4f}",
            "p95": f"{_quantile(rs, 0.95):.4f}",
            "positive": round(odds.positive, 4),
        },
        "history": {
            "entries": len(odds.history),
            "closed": len(closed),
            "won": sum(1 for t in closed if t.r is not None and t.r > ZERO),
            "mean_r": None if odds.history_mean_r is None else f"{odds.history_mean_r:.4f}",
        },
        "verdict": trade_verdict(odds),
    }


# -- the forward test -----------------------------------------------------


@dataclass
class ForwardOdds:
    #: The last real close.
    through: datetime
    #: The bar's last close, and its extension's.
    read_day: datetime
    extension_day: datetime
    #: Paper fills so far, and those ``rhca run`` logged a Robinhood quote for,
    #: counted through each deadline.
    fills_so_far: int
    priced_by_read: int
    priced_by_extension: int
    replays: int
    seed: int
    block_days: int
    sampled: tuple[datetime, datetime] | None
    #: Per replay, the priced fills by each deadline.
    at_read: list[int] = field(default_factory=list)
    at_extension: list[int] = field(default_factory=list)
    #: How many replays had each coin's entry or tranche by each deadline.
    lines: dict[tuple[str, str, str], int] = field(default_factory=dict)
    symbols: tuple[str, ...] = ()

    @property
    def simulated(self) -> bool:
        return bool(self.at_extension)

    def reached(self, counts: Sequence[int]) -> float:
        return _share(sum(c >= FILLS_NEEDED for c in counts), len(counts))

    def none(self, counts: Sequence[int], so_far: int) -> float:
        return _share(sum(c == so_far for c in counts), len(counts))

    def line(self, symbol: str, kind: str, deadline: str) -> float:
        return _share(self.lines.get((symbol, kind, deadline), 0), self.replays)


def _priced(logged: Mapping[str, Mapping[str, Any]], through: datetime) -> int:
    """Paper fills ``rhca run`` logged with a Robinhood quote, through a day."""
    return sum(
        1
        for day, record in logged.items()
        if day <= through.date().isoformat()
        for fill in record.get("fills") or []
        if fill.get("spread_pct") is not None
    )


def forward_test(
    config: AgentConfig,
    audit: AuditLog,
    bars: Bars,
    *,
    now: datetime,
    replays: int = DEFAULT_REPLAYS,
    seed: int = DEFAULT_SEED,
    block_days: int = DEFAULT_BLOCK_DAYS,
    sample_days: int = DEFAULT_SAMPLE_DAYS,
    progress: Callable[[str], None] | None = None,
) -> ForwardOdds:
    """The forward test's odds of reaching its priced fills, by its read and by
    its extension, over ``replays`` futures of every coin at once."""
    if replays < 1 or sample_days < 2:
        raise AgentError("--replays must be at least 1, --sample-days at least 2")
    shadow = shadow_mod._shadow(config)
    rule, _ = shadow_mod.rules(config)
    keep = shadow_mod.history_days(config, now=now)
    daily = {s: bars(s, max(keep, sample_days + 1)) for s in shadow.symbols}
    missing = sorted(s for s, b in daily.items() if not b)
    if missing:
        raise AgentError(f"Coinbase returned no daily bars for {', '.join(missing)}")
    through = min(latest_close(now), *(b[-1].start for b in daily.values()))
    daily = {s: [b for b in d if b.start <= through] for s, d in daily.items()}
    real = {s: d[-keep:] for s, d in daily.items()}
    read_day = shadow_mod.read_on(config) - DAY
    extension_day = shadow.start + DAY * (EXTENSION_CLOSES - 1)

    logged = shadow_mod.logged_days(audit, config)
    so_far = shadow_mod.replay(real, config, through=through)
    odds = ForwardOdds(
        through, read_day, extension_day,
        fills_so_far=len(so_far.fills),
        priced_by_read=_priced(logged, min(through, read_day)),
        priced_by_extension=_priced(logged, min(through, extension_day)),
        replays=replays if through < extension_day else 0,
        seed=seed, block_days=block_days, sampled=None, symbols=tuple(sorted(real)),
    )
    if through >= extension_day:
        return odds

    sampler = Sampler(
        {s: moves(_window(d, through, sample_days)) for s, d in daily.items()},
        block_days=block_days, seed=seed,
    )
    odds.sampled = (sampler.days[0], sampler.days[-1])
    views = {s: day_views(d, rule) for s, d in real.items()}
    ahead = (extension_day - through).days
    for index in range(replays):
        if progress and index and index % max(1, replays // 10) == 0:
            progress(f"  {index:,} of {replays:,} replays")
        drawn = sampler.draw(ahead)
        prepared = {}
        for symbol, bars_ in real.items():
            future = sampler.bars(bars_[-1], drawn)
            prepared[symbol] = Prepared(
                bars_ + future, {**views[symbol], **future_views(bars_, future, rule)}
            )
        split = shadow_mod.run_paper(prepared, config, through=extension_day)
        closes = {s: {b.start: b.close for b in p.daily} for s, p in prepared.items()}
        future_fills = [f for f in shadow_mod.paper_fills(split, closes) if f.day > through]
        by_read = [f for f in future_fills if f.day <= read_day]
        odds.at_read.append(odds.priced_by_read + len(by_read))
        odds.at_extension.append(odds.priced_by_extension + len(future_fills))
        for deadline, fills in (("read", by_read), ("extension", future_fills)):
            for key in {(f.symbol, _KINDS[(f.sleeve, f.side)], deadline) for f in fills}:
                odds.lines[key] = odds.lines.get(key, 0) + 1
    return odds


def forward_verdict(odds: ForwardOdds) -> str:
    if odds.priced_by_read >= FILLS_NEEDED:
        return (
            f"Already {odds.priced_by_read} priced fills: the bar can be scored at its first "
            "read."
        )
    if not odds.simulated:
        return "Past its extension: nothing left to replay."
    read, ext = odds.reached(odds.at_read), odds.reached(odds.at_extension)
    if odds.through >= odds.read_day:
        return f"Past its first read; by its extension, {_rate(ext)} of replays reach it."
    if read >= 0.7:
        head = "Likely conclusive at the first read"
    elif read >= 0.3:
        head = "A toss-up at the first read"
    else:
        head = "Likely inconclusive at the first read"
    return f"{head}: {_rate(read)} of replays reach {FILLS_NEEDED} priced fills; by the extension, {_rate(ext)}."


def render_forward(odds: ForwardOdds, config: AgentConfig) -> str:
    shadow = shadow_mod._shadow(config)
    lines = [
        f"Time Machine: the forward test's odds of {FILLS_NEEDED} priced paper fills",
        f"  It counts from the {shadow.start:%Y-%m-%d} close. Its bar is read on the "
        f"{odds.read_day:%Y-%m-%d} close ({shadow_mod.BAR_DAYS} closes), with one extension "
        f"to the {odds.extension_day:%Y-%m-%d} close ({EXTENSION_CLOSES}).",
        f"  Through the {odds.through:%Y-%m-%d} close: {odds.fills_so_far} paper fill(s), "
        f"{odds.priced_by_extension} with a Robinhood quote.",
    ]
    if odds.simulated and odds.sampled:
        first, last = odds.sampled
        ahead = (odds.extension_day - odds.through).days
        lines += [
            "",
            f"{odds.replays:,} replays of the {ahead} closes to {odds.extension_day:%Y-%m-%d}, "
            f"every coin drawing the same past days in blocks (mean {odds.block_days} days) "
            f"from {first:%Y-%m-%d} to {last:%Y-%m-%d}, seed {odds.seed}.",
        ]
        deadlines = [("extension", odds.extension_day, odds.at_extension, odds.priced_by_extension)]
        if odds.through < odds.read_day:
            deadlines.insert(0, ("read", odds.read_day, odds.at_read, odds.priced_by_read))
        for _, day, counts, so_far in deadlines:
            lines.append(
                f"  by the {day:%Y-%m-%d} close: {FILLS_NEEDED}+ priced fills "
                f"{_rate(odds.reached(counts)):>7}   no new fill {_rate(odds.none(counts, so_far)):>7}"
                f"   median {statistics.median(counts):.0f}"
            )
        header = "".join(
            f"{'entry by ' + day.strftime('%m-%d'):>16}{'tranche by ' + day.strftime('%m-%d'):>18}"
            for _, day, _, _ in deadlines
        )
        lines += ["", f"  {'coin':<9}{header}"]
        for symbol in odds.symbols:
            lines.append(
                f"  {symbol:<9}" + "".join(
                    f"{_rate(odds.line(symbol, ENTRY, name)):>16}"
                    f"{_rate(odds.line(symbol, TRANCHE, name)):>18}"
                    for name, _, _, _ in deadlines
                )
            )
        lines.append(
            "  Assumes rhca run records every close from here: a close it misses has no "
            "quote, so its fill would not count."
        )
    lines += ["", forward_verdict(odds),
              "Odds from the coins' own past days, not a forecast. The paper fills here are "
              "not proposals, and nothing here approves anything."]
    return "\n".join(lines)


def forward_summary(odds: ForwardOdds) -> dict[str, Any]:
    deadlines = {"extension": (odds.extension_day, odds.at_extension, odds.priced_by_extension)}
    if odds.through < odds.read_day:
        deadlines = {"read": (odds.read_day, odds.at_read, odds.priced_by_read), **deadlines}
    return {
        "through": odds.through.date().isoformat(),
        "read_day": odds.read_day.date().isoformat(),
        "extension_day": odds.extension_day.date().isoformat(),
        "fills_so_far": odds.fills_so_far,
        "priced_so_far": odds.priced_by_extension,
        "replays": odds.replays,
        "seed": odds.seed,
        "block_days": odds.block_days,
        "sampled": (
            [odds.sampled[0].date().isoformat(), odds.sampled[1].date().isoformat()]
            if odds.sampled else None
        ),
        "deadlines": {
            name: {
                "day": day.date().isoformat(),
                "reached": round(odds.reached(counts), 4),
                "no_new_fill": round(odds.none(counts, so_far), 4),
                "lines": {
                    symbol: {
                        kind: round(odds.line(symbol, kind, name), 4)
                        for kind in (ENTRY, STOP, TRANCHE)
                    }
                    for symbol in odds.symbols
                },
            }
            for name, (day, counts, so_far) in deadlines.items()
            if odds.simulated
        },
        "verdict": forward_verdict(odds),
    }
