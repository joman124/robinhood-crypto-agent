"""The candidates of ``docs/candidates.md``, run against their bar at several costs.

The live split decides once a day because nothing faster has paid at
Robinhood's ~1.9% round trip. This asks the next question: **at what round
trip would a faster rule pay?** Every candidate (``strategy/candidates.py``)
runs as one account across its coins, stepped a bar at a time on its own bar
size, at each round trip swept, and is scored on the six checks the document
fixed before any run.

Costs come two ways. A **taker** fill crosses half the round trip at the close
of the bar that decided it, as every backtest here does. A **maker** entry
rests as a limit at that close, pays ``maker_pct``, and fills only if a later
bar trades *below* it within ``maker_bars`` bars -- so the breakouts that run
away at once are missed, and the fills come on pullbacks. Exits are always
taker: a stop is a taker order.

The engine is the breakout's one-account backtest (``portfolio_backtest``)
generalized to any bar: with the live breakout on daily bars and taker costs
it makes the same trades, which a test holds it to. Like every backtest here,
a result authorizes nothing.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .backtest import STRESS_FACTOR, money, pct, window_starts
from .models import Candle
from .numeric import ZERO
from .portfolio_backtest import (
    DEFAULT_CAPITAL,
    DEFAULT_MIN_TRADE,
    PortfolioResult,
    Trade,
    combine,
    run_equal_hold,
)
from .portfolio_backtest import Prepared as DailyPrepared
from .strategy.breakout import daily_bars
from .strategy.candidates import Candidate, Held, Rule

ONE_HUNDRED = Decimal("100")
#: The round trips swept by default: today's measured spread, then cheaper.
DEFAULT_ROUND_TRIPS = (Decimal("1.9"), Decimal("1.0"), Decimal("0.5"), Decimal("0.2"))
DEFAULT_MAKER_BARS = 3
#: The expectancy the edge check asks for, in R.
MIN_EXPECTANCY_R = Decimal("0.2")
YEAR = timedelta(days=365)
DEFAULT_WINDOW = timedelta(days=90)
DEFAULT_STEP = timedelta(days=30)


@dataclass(frozen=True)
class Costs:
    #: Percent; every taker fill crosses half of it.
    round_trip_pct: Decimal
    #: Percent a resting entry pays instead; ``None`` enters as a taker.
    maker_pct: Decimal | None = None
    #: Bars a resting entry waits for a fill before it is canceled.
    maker_bars: int = DEFAULT_MAKER_BARS

    def __post_init__(self) -> None:
        if self.round_trip_pct < ZERO or (self.maker_pct is not None and self.maker_pct < ZERO):
            raise ValueError("costs cannot be negative")
        if self.maker_bars < 1:
            raise ValueError("a resting entry must wait at least one bar")

    @property
    def taker(self) -> Decimal:
        """One taker fill's cost, as a fraction."""
        return self.round_trip_pct / Decimal(200)

    @property
    def maker(self) -> Decimal | None:
        return None if self.maker_pct is None else self.maker_pct / ONE_HUNDRED

    def stressed(self) -> Costs:
        return replace(
            self,
            round_trip_pct=self.round_trip_pct * STRESS_FACTOR,
            maker_pct=None if self.maker_pct is None else self.maker_pct * STRESS_FACTOR,
        )

    def label(self) -> str:
        return f"{self.round_trip_pct}%"


@dataclass
class Prepared:
    """One coin's bars on a rule's bar size, and the rule's view of each."""

    bars: list[Candle]
    views: dict[datetime, Any]
    by_start: dict[datetime, Candle] = field(init=False)

    def __post_init__(self) -> None:
        self.by_start = {bar.start: bar for bar in self.bars}


@dataclass
class Market:
    """A rule's prepared coins, and every bar start across them in order."""

    rule: Rule
    prepared: dict[str, Prepared]
    moments: list[datetime]
    span: timedelta

    @classmethod
    def build(cls, rule: Rule, series: Mapping[str, Sequence[Candle]]) -> Market:
        prepared = {}
        for symbol, hourly in series.items():
            bars, views = rule.prepare(hourly)
            prepared[symbol] = Prepared(bars, views)
        return cls._of(rule, prepared)

    @classmethod
    def _of(cls, rule: Rule, prepared: dict[str, Prepared]) -> Market:
        moments = sorted({bar.start for p in prepared.values() for bar in p.bars})
        return cls(rule, prepared, moments, timedelta(minutes=rule.interval_minutes))

    def without(self, symbol: str) -> Market:
        return self._of(self.rule, {s: p for s, p in self.prepared.items() if s != symbol})

    def between(self, start: datetime | None, end: datetime | None) -> list[datetime]:
        """Bar starts of bars wholly inside ``[start, end)``."""
        low = 0 if start is None else bisect.bisect_left(self.moments, start)
        high = len(self.moments) if end is None else bisect.bisect_right(self.moments, end - self.span)
        return self.moments[low:high]


@dataclass
class Run(PortfolioResult):
    #: Resting entries canceled unfilled, and entries the cost filter turned away.
    missed: int = 0
    filtered: int = 0


@dataclass
class _Resting:
    dollars: Decimal
    limit: Decimal
    risk: Decimal
    view: Any
    waited: int = 0


class _Account:
    """One account's cash, positions and resting entries, stepped a bar at a time."""

    def __init__(
        self,
        market: Market,
        *,
        costs: Costs,
        capital: Decimal,
        min_trade: Decimal,
        min_stop_trips: Decimal | None,
        name: str,
    ) -> None:
        self.market = market
        self.rule = market.rule
        self.costs = costs
        self.taker = costs.taker
        self.maker = costs.maker
        self.min_trade = min_trade
        self.min_stop = (
            None if min_stop_trips is None else min_stop_trips * costs.round_trip_pct / ONE_HUNDRED
        )
        self.symbols = sorted(market.prepared)
        self.result = Run(name, self.symbols, capital, costs.round_trip_pct)
        self.cash = capital
        self.reserved = ZERO
        self.positions: dict[str, Held] = {}
        self.resting: dict[str, _Resting] = {}
        self.last_close: dict[str, Decimal] = {}

    def value(self) -> Decimal:
        """The coins held, at the bid."""
        return sum(
            (h.quantity * self.last_close[s] * (1 - self.taker) for s, h in self.positions.items()),
            ZERO,
        )

    def equity(self) -> Decimal:
        return self.cash + self.reserved + self.value()

    def step(self, moment: datetime) -> None:
        bars = {}
        for symbol in self.symbols:
            bar = self.market.prepared[symbol].by_start.get(moment)
            if bar is not None:
                bars[symbol] = bar
                self.last_close[symbol] = bar.close
        self._fill(moment, bars)
        self._exit(moment)
        self._enter(moment)
        account = self.equity()
        self.result.equity_curve.append((moment, account))
        in_coins = account - self.cash - self.reserved
        self.result.exposure.append(in_coins / account if account > ZERO else ZERO)

    def _fill(self, moment: datetime, bars: Mapping[str, Candle]) -> None:
        """A resting entry fills at its limit on a bar that trades below it."""
        for symbol in sorted(self.resting):
            bar = bars.get(symbol)
            if bar is None:
                continue
            order = self.resting[symbol]
            if bar.low < order.limit:
                assert self.maker is not None
                del self.resting[symbol]
                self.reserved -= order.dollars
                self.positions[symbol] = Held(
                    symbol=symbol,
                    quantity=order.dollars / (order.limit * (1 + self.maker)),
                    entered=moment,
                    entry_price=order.limit,
                    cost=order.dollars,
                    risk=order.risk,
                    highest=order.limit,
                    entry_view=order.view,
                )
                continue
            order.waited += 1
            if order.waited >= self.costs.maker_bars:
                del self.resting[symbol]
                self.reserved -= order.dollars
                self.cash += order.dollars
                self.result.missed += 1

    def _exit(self, moment: datetime) -> None:
        for symbol in sorted(self.positions):
            view = self.market.prepared[symbol].views.get(moment)
            if view is None:
                continue
            held = self.positions[symbol]
            if held.entered < moment:
                held.bars += 1
            held.highest = max(held.highest, view.close)
            if self.rule.exits(held, view):
                proceeds = held.quantity * view.close * (1 - self.taker)
                self.cash += proceeds
                self.result.trades.append(
                    Trade(symbol, held.entered, moment, held.quantity, held.cost, proceeds, held.risk)
                )
                del self.positions[symbol]

    def _enter(self, moment: datetime) -> None:
        """Every entry on a bar sizes off the account after that bar's exits."""
        account = self.equity()
        for symbol in self.symbols:
            if symbol in self.positions or symbol in self.resting:
                continue
            view = self.market.prepared[symbol].views.get(moment)
            if view is None or not self.rule.enters(view):
                continue
            fraction = self.rule.stop_fraction(view)
            if self.min_stop is not None and fraction < self.min_stop:
                self.result.filtered += 1
                continue
            dollars = min(self.rule.position_dollars(account, view), self.cash)
            if dollars < self.min_trade:
                continue
            self.cash -= dollars
            risk = dollars * fraction
            if self.maker is None:
                self.positions[symbol] = Held(
                    symbol=symbol,
                    quantity=dollars / (view.close * (1 + self.taker)),
                    entered=moment,
                    entry_price=view.close,
                    cost=dollars,
                    risk=risk,
                    highest=view.close,
                    entry_view=view,
                )
            else:
                self.reserved += dollars
                self.resting[symbol] = _Resting(dollars, view.close, risk, view)

    def finish(self) -> Run:
        for symbol, held in sorted(self.positions.items()):
            proceeds = held.quantity * self.last_close[symbol] * (1 - self.taker)
            self.result.trades.append(
                Trade(symbol, held.entered, None, held.quantity, held.cost, proceeds, held.risk)
            )
        return self.result


def run(
    market: Market,
    *,
    costs: Costs,
    capital: Decimal = DEFAULT_CAPITAL,
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    min_stop_trips: Decimal | None = None,
    name: str | None = None,
) -> Run:
    """Trade ``market``'s rule on every coin in it, from one account.

    Only bars inside ``[start, end)`` are traded; the indicators read every
    bar before them, so a window starts with them already warm.
    """
    account = _Account(
        market, costs=costs, capital=capital, min_trade=min_trade,
        min_stop_trips=min_stop_trips, name=name or market.rule.name,
    )
    for moment in market.between(start, end):
        account.step(moment)
    return account.finish()


def run_candidate(
    markets: Sequence[Market],
    candidate: Candidate,
    *,
    costs: Costs,
    capital: Decimal = DEFAULT_CAPITAL,
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
) -> Run:
    """One candidate: its one rule, or its sleeves on equal shares of ``capital``
    combined into one account. ``markets`` holds one per sleeve, in order."""
    kwargs: dict[str, Any] = dict(
        costs=costs, start=start, end=end, min_trade=min_trade,
        min_stop_trips=candidate.min_stop_trips,
    )
    if len(markets) == 1:
        return run(markets[0], capital=capital, name=candidate.name, **kwargs)
    share = capital / Decimal(len(markets))
    parts = [run(market, capital=share, **kwargs) for market in markets]
    combined = combine(candidate.name, parts)
    return Run(
        combined.name, combined.symbols, combined.capital, combined.round_trip_pct,
        trades=combined.trades, equity_curve=combined.equity_curve, exposure=combined.exposure,
        missed=sum(p.missed for p in parts), filtered=sum(p.filtered for p in parts),
    )


# -- scoring -----------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    """A run's numbers, kept once its equity curve is no longer needed."""

    return_pct: Decimal
    max_drawdown_pct: Decimal
    return_over_drawdown: Decimal | None
    closed: int
    open: int
    win_rate: float | None
    expectancy_r: Decimal | None
    exposure_pct: Decimal
    pnl_by_symbol: dict[str, Decimal]
    missed: int = 0
    filtered: int = 0

    @classmethod
    def of(cls, result: PortfolioResult) -> Stats:
        return cls(
            return_pct=result.total_return_pct,
            max_drawdown_pct=result.max_drawdown_pct,
            return_over_drawdown=result.return_over_drawdown,
            closed=len(result.closed),
            open=sum(1 for t in result.trades if t.open),
            win_rate=result.win_rate,
            expectancy_r=result.expectancy_r,
            exposure_pct=result.average_exposure_pct,
            pnl_by_symbol=result.pnl_by_symbol(),
            missed=getattr(result, "missed", 0),
            filtered=getattr(result, "filtered", 0),
        )


@dataclass(frozen=True)
class Check:
    key: str
    name: str
    #: True passed, False failed, None could not be run.
    passed: bool | None
    detail: str


CHECK_NAMES = {
    "1": "edge", "2": "risk", "3": "bad year", "4": "consistency", "5": "breadth", "6": "plateau",
}


@dataclass
class Verdict:
    candidate: Candidate
    costs: Costs
    symbols: list[str]
    base: Stats
    stressed: Stats
    hold: Stats
    checks: list[Check]

    @property
    def status(self) -> str:
        if any(c.passed is False for c in self.checks):
            return "fail"
        if any(c.passed is None for c in self.checks):
            return "incomplete"
        return "PASS"

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)


def _better(value: Decimal | None, than: Decimal | None) -> bool:
    if value is None:
        return False
    return than is None or value > than


def _edge(candidate: Candidate, base: Stats, stressed: Stats) -> Check:
    expectancy = base.expectancy_r
    passed = (
        base.closed >= candidate.min_trades
        and expectancy is not None
        and expectancy > MIN_EXPECTANCY_R
        and stressed.return_pct > ZERO
    )
    return Check(
        "1", "edge", passed,
        f"{base.closed} closed trades (needs {candidate.min_trades}) at {_r(expectancy)} "
        f"(needs above +{MIN_EXPECTANCY_R}R); stressed {pct(stressed.return_pct)}",
    )


def _risk(base: Stats, hold: Stats) -> Check:
    passed = base.max_drawdown_pct <= hold.max_drawdown_pct / 2 and _better(
        base.return_over_drawdown, hold.return_over_drawdown
    )
    return Check(
        "2", "risk", passed,
        f"max drawdown {pct(-base.max_drawdown_pct)} vs hold's {pct(-hold.max_drawdown_pct)}; "
        f"return over drawdown {_ratio(base.return_over_drawdown)} vs "
        f"{_ratio(hold.return_over_drawdown)}",
    )


def _bad_year(year: Stats, hold: Stats) -> Check:
    made, held = year.return_pct, hold.return_pct
    passed = made > ZERO or (held < ZERO and made > held / 2)
    return Check(
        "3", "bad year", passed, f"last 365 days {pct(made)} vs hold's {pct(held)}"
    )


def _consistency(returns: Sequence[float], hold_returns: Sequence[float]) -> Check:
    if not returns:
        return Check("4", "consistency", None, "no whole rolling window fits in the history")
    positive = sum(1 for r in returns if r > 0)
    worst, hold_worst = min(returns), min(hold_returns)
    passed = positive * 2 > len(returns) and worst >= min(hold_worst, 0.0) / 2
    return Check(
        "4", "consistency", passed,
        f"made money in {positive}/{len(returns)} windows; worst {pct(worst)} vs hold's worst "
        f"{pct(hold_worst)}; median {pct(statistics.median(returns))}",
    )


def _breadth(best: str | None, rest: Stats | None) -> Check:
    if best is None or rest is None:
        return Check("5", "breadth", None, "one coin: nothing to leave out")
    return Check(
        "5", "breadth", rest.return_pct > ZERO,
        f"without {best}: {pct(rest.return_pct)} at {_r(rest.expectancy_r)}",
    )


def _plateau(candidate: Candidate, neighbours: Sequence[tuple[str, Stats]]) -> Check:
    if not candidate.neighbours:
        return Check("6", "plateau", True, "n/a: it is itself an average over its parameter")
    passed = all(s.expectancy_r is not None and s.expectancy_r > ZERO for _, s in neighbours)
    return Check(
        "6", "plateau", passed,
        "; ".join(f"{name} {_r(s.expectancy_r)}" for name, s in neighbours),
    )


@dataclass
class Report:
    costs: list[Costs]
    candidates: list[Candidate]
    #: One verdict per cost, in ``costs`` order, by candidate key.
    verdicts: dict[str, list[Verdict]]
    start: datetime | None
    end: datetime | None
    capital: Decimal
    window: timedelta | None
    step: timedelta | None
    #: Coins a candidate wanted that had no history.
    missing: dict[str, list[str]] = field(default_factory=dict)


Progress = Callable[[str], None]


class Suite:
    """Every candidate at every cost, sharing prepared bars between them."""

    def __init__(
        self,
        series: Mapping[str, Sequence[Candle]],
        *,
        symbols: Sequence[str],
        capital: Decimal = DEFAULT_CAPITAL,
        start: datetime | None = None,
        min_trade: Decimal = DEFAULT_MIN_TRADE,
        window: timedelta | None = DEFAULT_WINDOW,
        step: timedelta = DEFAULT_STEP,
        progress: Progress | None = None,
    ) -> None:
        self.series = series
        self.symbols = [s for s in symbols if s in series]
        self.capital = capital
        self.start = start
        self.min_trade = min_trade
        self.window = window
        self.step = step
        self.progress = progress
        self.end = max((bars[-1].end for bars in series.values() if bars), default=None)
        self._markets: dict[tuple[Any, tuple[str, ...]], Market] = {}
        self._holds: dict[tuple[str, ...], dict[str, DailyPrepared]] = {}
        self._windows: dict[tuple[str, ...], list[datetime]] = {}

    def coins(self, candidate: Candidate) -> tuple[str, ...]:
        wanted = candidate.symbols or tuple(self.symbols)
        return tuple(sorted(s for s in wanted if s in self.series))

    def markets(self, candidate: Candidate, coins: tuple[str, ...]) -> list[Market]:
        out = []
        for rule in candidate.sleeves:
            key = (rule, coins)
            if key not in self._markets:
                self._markets[key] = Market.build(rule, {s: self.series[s] for s in coins})
            out.append(self._markets[key])
        return out

    def _hold(self, coins: tuple[str, ...]) -> dict[str, DailyPrepared]:
        if coins not in self._holds:
            self._holds[coins] = {
                s: DailyPrepared(daily_bars(self.series[s]), {}) for s in coins
            }
        return self._holds[coins]

    def _window_starts(self, coins: tuple[str, ...], window: timedelta) -> list[datetime]:
        """Rolling windows inside the tested span, so each is a whole window of it."""
        if coins not in self._windows:
            inside = {
                s: [c for c in self.series[s] if self.start is None or c.start >= self.start]
                for s in coins
            }
            self._windows[coins] = window_starts(inside, window=window, step=self.step)
        return self._windows[coins]

    def _say(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def evaluate(self, candidate: Candidate, costs: Costs) -> Verdict:
        coins = self.coins(candidate)
        markets = self.markets(candidate, coins)
        hold_prepared = self._hold(coins)
        span = dict(capital=self.capital, min_trade=self.min_trade)

        def candidate_run(
            cand: Candidate, *, ms: Sequence[Market] = markets, at: Costs = costs,
            start: datetime | None = self.start, end: datetime | None = None,
        ) -> Run:
            return run_candidate(ms, cand, costs=at, start=start, end=end, **span)

        def hold_run(start: datetime | None, end: datetime | None = None) -> Stats:
            return Stats.of(
                run_equal_hold(
                    hold_prepared, capital=self.capital, round_trip_pct=costs.round_trip_pct,
                    start=start, end=end,
                )
            )

        base_run = candidate_run(candidate)
        base = Stats.of(base_run)
        del base_run
        stressed = Stats.of(candidate_run(candidate, at=costs.stressed()))
        hold = hold_run(self.start)

        year_start = None if self.end is None else self.end - YEAR
        if self.start is not None and year_start is not None:
            year_start = max(year_start, self.start)
        year = Stats.of(candidate_run(candidate, start=year_start))
        hold_year = hold_run(year_start)

        returns: list[float] = []
        hold_returns: list[float] = []
        if self.window is not None:
            for window_start in self._window_starts(coins, self.window):
                window_end = window_start + self.window
                window_run = candidate_run(candidate, start=window_start, end=window_end)
                returns.append(float(window_run.total_return_pct))
                hold_returns.append(float(hold_run(window_start, window_end).return_pct))

        best: str | None = None
        rest: Stats | None = None
        if len(coins) > 1 and base.pnl_by_symbol:
            best = max(base.pnl_by_symbol, key=lambda s: base.pnl_by_symbol[s])
            rest = Stats.of(candidate_run(candidate, ms=[m.without(best) for m in markets]))

        neighbours = [
            (n.name, Stats.of(candidate_run(n, ms=self.markets(n, self.coins(n)))))
            for n in candidate.neighbours
        ]
        return Verdict(
            candidate=candidate,
            costs=costs,
            symbols=list(coins),
            base=base,
            stressed=stressed,
            hold=hold,
            checks=[
                _edge(candidate, base, stressed),
                _risk(base, hold),
                _bad_year(year, hold_year),
                _consistency(returns, hold_returns),
                _breadth(best, rest),
                _plateau(candidate, neighbours),
            ],
        )

    def run(self, candidates: Sequence[Candidate], costs: Sequence[Costs]) -> Report:
        report = Report(
            costs=list(costs), candidates=list(candidates), verdicts={}, start=self.start,
            end=self.end, capital=self.capital, window=self.window, step=self.step,
        )
        for candidate in candidates:
            wanted = candidate.symbols or tuple(self.symbols)
            missing = sorted(set(wanted) - set(self.series))
            if missing:
                report.missing[candidate.key] = missing
            if not self.coins(candidate):
                continue
            for cost in costs:
                self._say(f"  {candidate.key} {candidate.name} at {cost.label()} ...")
                report.verdicts.setdefault(candidate.key, []).append(self.evaluate(candidate, cost))
        return report


def sweep(
    round_trips: Sequence[Decimal],
    *,
    maker_pct: Decimal | None = None,
    maker_bars: int = DEFAULT_MAKER_BARS,
) -> list[Costs]:
    return [Costs(trip, maker_pct, maker_bars) for trip in round_trips]


# -- reporting ---------------------------------------------------------------


def _r(value: Decimal | None) -> str:
    return "-" if value is None else f"{float(value):+.2f}R"


def _ratio(value: Decimal | None) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _label(candidate: Candidate) -> str:
    return f"{candidate.key:<4}{candidate.name}"


_WIDTH = 30


def pays_up_to(verdicts: Sequence[Verdict]) -> tuple[Decimal | None, Decimal | None]:
    """The highest round trip swept at which the expectancy is positive, and at
    which every check passes; ``None`` where none is."""
    pays = [
        v.costs.round_trip_pct
        for v in verdicts
        if v.base.expectancy_r is not None and v.base.expectancy_r > ZERO
    ]
    clears = [v.costs.round_trip_pct for v in verdicts if v.status == "PASS"]
    return (max(pays) if pays else None), (max(clears) if clears else None)


def render(report: Report, *, verbose: bool = False) -> str:
    span = (
        f", {report.start:%Y-%m-%d} to {report.end:%Y-%m-%d}"
        if report.start and report.end
        else ""
    )
    maker = report.costs[0] if report.costs else None
    lines = [
        "=" * 110,
        "CANDIDATES AGAINST THEIR BAR (docs/candidates.md)",
        "=" * 110,
        f"Each candidate is one {money(report.capital)} account{span}, long only, deciding on "
        "closed bars.",
    ]
    if maker is not None and maker.maker_pct is not None:
        lines.append(
            f"Entries rest as limits at the deciding close, pay {maker.maker_pct}%, and fill only "
            f"if a later bar trades below them within {maker.maker_bars} bars; exits cross half "
            "the round trip."
        )
    else:
        lines.append("Every fill crosses half the round trip at the close of the bar that decided it.")
    lines += [
        f"'stressed' re-runs at {STRESS_FACTOR}x the costs. Checks: "
        + ", ".join(f"{k} {name}" for k, name in CHECK_NAMES.items())
        + ".",
        "Each cell: expectancy in R after costs, checks passed of 6, and the verdict.",
        "",
        f"  {'candidate':<{_WIDTH}}"
        + "".join(f"{c.label() + ' round trip':>20}" for c in report.costs),
    ]
    for candidate in report.candidates:
        verdicts = report.verdicts.get(candidate.key)
        if not verdicts:
            lines.append(f"  {_label(candidate):<{_WIDTH}}  no history for its coins")
            continue
        lines.append(
            f"  {_label(candidate):<{_WIDTH}}"
            + "".join(
                f"{f'{_r(v.base.expectancy_r)} {v.passed}/6 {v.status}':>20}" for v in verdicts
            )
        )
    lines += ["", "  Where each one pays (expectancy above zero) and clears its bar, of the costs swept:"]
    for candidate in report.candidates:
        verdicts = report.verdicts.get(candidate.key)
        if not verdicts:
            continue
        pays, clears = pays_up_to(verdicts)
        lines.append(
            f"  {_label(candidate):<{_WIDTH}}"
            + (f"pays up to {pays}%" if pays is not None else "pays at none")
            + "; "
            + (f"clears the bar up to {clears}%" if clears is not None else "clears the bar at none")
        )
    for index, cost in enumerate(report.costs):
        lines += [
            "",
            f"At a {cost.label()} round trip"
            + (f" (entries resting at {cost.maker_pct}%)" if cost.maker_pct is not None else ""),
            f"  {'candidate':<{_WIDTH}}{'return':>9}{'max dd':>9}{'ret/dd':>8}{'trades':>8}"
            f"{'win rate':>10}{'expectancy':>12}{'in coins':>10}{'stressed':>10}  failed",
        ]
        holds: dict[tuple[str, ...], Stats] = {}
        for candidate in report.candidates:
            verdicts = report.verdicts.get(candidate.key)
            if not verdicts:
                continue
            v = verdicts[index]
            holds.setdefault(tuple(v.symbols), v.hold)
            failed = ", ".join(
                f"{c.key} {c.name}" + ("?" if c.passed is None else "")
                for c in v.checks
                if not c.passed
            )
            lines.append(_stats_line(_label(candidate), v.base, v.stressed) + f"  {failed or '-'}")
            if verbose:
                for check in v.checks:
                    mark = {True: "pass", False: "FAIL", None: "not run"}[check.passed]
                    lines.append(f"        {check.key} {check.name:<12}{mark:<9}{check.detail}")
                extra = []
                if v.base.missed:
                    extra.append(f"{v.base.missed} resting entries never filled")
                if v.base.filtered:
                    extra.append(f"the cost filter turned entries away {v.base.filtered} times")
                if extra:
                    lines.append("        " + "; ".join(extra))
        for coins, hold in holds.items():
            count = len(coins)
            name = f"hold ({count} coin{'' if count == 1 else 's'}, equal weight)"
            lines.append(_stats_line(name, hold, None))
    for key, missing in report.missing.items():
        lines.append(f"Candidate {key} had no history for {', '.join(missing)}; it ran without them.")
    lines += [
        "",
        "A backtest is evidence about rules on past prices, not about the next trade: it",
        "authorizes nothing. Read each candidate against the bar docs/candidates.md fixed before",
        "any run, and record the result there.",
    ]
    return "\n".join(lines)


def _stats_line(name: str, stats: Stats, stressed: Stats | None) -> str:
    return (
        f"  {name:<{_WIDTH}}{pct(stats.return_pct):>9}{pct(-stats.max_drawdown_pct):>9}"
        f"{_ratio(stats.return_over_drawdown):>8}{stats.closed:>8}{_rate(stats.win_rate):>10}"
        f"{_r(stats.expectancy_r):>12}{float(stats.exposure_pct):>9.0f}%"
        f"{pct(stressed.return_pct) if stressed else '-':>10}"
    )


def _num(value: Decimal | None, places: int = 2) -> str | None:
    return None if value is None else f"{value:.{places}f}"


def _stats(stats: Stats) -> dict[str, object]:
    return {
        "return_pct": _num(stats.return_pct),
        "max_drawdown_pct": _num(stats.max_drawdown_pct),
        "return_over_drawdown": _num(stats.return_over_drawdown),
        "closed_trades": stats.closed,
        "open_trades": stats.open,
        "win_rate": stats.win_rate,
        "expectancy_r": _num(stats.expectancy_r, 3),
        "average_exposure_pct": _num(stats.exposure_pct),
        "pnl_by_symbol": {s: _num(v) for s, v in stats.pnl_by_symbol.items()},
        "missed_entries": stats.missed,
        "filtered_entries": stats.filtered,
    }


def summary(report: Report) -> dict[str, object]:
    """The report's numbers, JSON-safe."""
    out: dict[str, object] = {
        "start": report.start.isoformat() if report.start else None,
        "end": report.end.isoformat() if report.end else None,
        "capital": _num(report.capital),
        "round_trips_pct": [str(c.round_trip_pct) for c in report.costs],
        "maker_pct": (
            str(report.costs[0].maker_pct)
            if report.costs and report.costs[0].maker_pct is not None
            else None
        ),
        "maker_bars": report.costs[0].maker_bars if report.costs else None,
        "missing": report.missing,
        "candidates": {},
    }
    candidates: dict[str, object] = {}
    for candidate in report.candidates:
        verdicts = report.verdicts.get(candidate.key, [])
        pays, clears = pays_up_to(verdicts)
        candidates[candidate.key] = {
            "name": candidate.name,
            "pays_up_to_pct": str(pays) if pays is not None else None,
            "clears_bar_up_to_pct": str(clears) if clears is not None else None,
            "by_round_trip": {
                str(v.costs.round_trip_pct): {
                    "status": v.status,
                    "symbols": v.symbols,
                    "candidate": _stats(v.base),
                    "stressed": _stats(v.stressed),
                    "hold": _stats(v.hold),
                    "checks": [
                        {"key": c.key, "name": c.name, "passed": c.passed, "detail": c.detail}
                        for c in v.checks
                    ],
                }
                for v in verdicts
            },
        }
    out["candidates"] = candidates
    return out
