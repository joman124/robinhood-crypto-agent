"""Backtests over hourly bars, under the round trip Robinhood actually charges.

Three strategies on the same bars:

``ladder``
    The trend ladder the agent trades (:mod:`strategy.ladder`), driven through
    the same :meth:`Ladder.decide` the live pipeline calls. It runs in both
    sell modes, ``lot`` and ``anchor``, and with a trend window
    (``--trend-days N``) twice more: ``+Nd`` buys only on a bar that closed
    above the average of the last N days of closes, and ``+Nd exit`` also sells
    everything on a bar that closed at or under it. The average is warmed up on
    N days of bars fetched before the window, so the window itself is the same
    as without the filter.

``trend``
    The plainest use of the same average: buy $100 on a close above it, sell
    everything on a close at or under it. It trades a few times a year, so the
    spread barely matters, and it asks whether the ladder adds anything to the
    trend filter alone.

``hold``
    Buy once on the first bar and hold: the baseline everything has to beat.

Every trade crosses the spread: a buy pays the mark plus half of it, a sell
gets the mark less half, at bar closes. The report counts what a win rate
hides. Positions still open at the end are marked to the bid and shown
separately, and a second win rate counts them. The worst drawdown and the
most capital tied up are reported next to the profit. A ladder can close
nearly every trade at a profit while holding a large loss it never sold.

**Rolling windows** (``--roll-window``) re-run every strategy over many
windows of the same length, each starting flat, so one lucky or unlucky start
date cannot carry the verdict.

The breakout candidate sizes each trade from one account's equity, so it is
backtested across all the coins at once, in ``portfolio_backtest``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Mapping, Sequence

from .models import Candle, Side
from .numeric import ZERO, round_money
from .strategy.ladder import (
    DEFAULT_STEPS,
    MODE_ANCHOR,
    MODE_LOT,
    MODES,
    HeldLot,
    Ladder,
    above_trend,
    describe_steps,
    parse_steps,
    trend_bars,
    trend_lookup,
)
from .symbols import canonical

if TYPE_CHECKING:
    from .config import AgentConfig

__all__ = [
    "DEFAULT_LADDER",
    "above_trend",
    "parse_ladder",
    "trend_bars",
]

#: (percent from the anchor, dollars) -- the steps the owner proposed.
DEFAULT_LADDER = DEFAULT_STEPS
parse_ladder = parse_steps
#: The round trip measured on Robinhood's market-maker quotes, 2026-09.
DEFAULT_ROUND_TRIP_PCT = Decimal("1.9")
LADDER_MODES = MODES
#: What the buy-and-hold and trend baselines invest.
HOLD_DOLLARS = Decimal("100")


@dataclass
class Lot:
    quantity: Decimal
    price: Decimal
    opened_at: datetime
    opened_index: int
    #: The mark it was bought at, and the ladder step that bought it.
    mark: Decimal = ZERO
    step: int = -1


@dataclass(frozen=True)
class Fill:
    """One order, as filled: when, which way, how much, at what price."""

    at: datetime
    side: Side
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class ClosedTrade:
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    entry: Decimal
    exit: Decimal

    @property
    def pnl(self) -> Decimal:
        return self.quantity * (self.exit - self.entry)

    @property
    def return_pct(self) -> Decimal:
        return (self.exit / self.entry - Decimal(1)) * Decimal(100)


class Book:
    """Lots, fills and an equity curve, with the spread charged on every fill."""

    def __init__(self, half_spread_pct: Decimal) -> None:
        self.half = half_spread_pct / Decimal(100)
        self.lots: list[Lot] = []
        self.closed: list[ClosedTrade] = []
        self.buys = 0
        self.sells = 0
        self.max_capital = ZERO
        self.peak_equity = ZERO
        self.max_drawdown = ZERO
        self.equity_curve: list[tuple[datetime, Decimal]] = []
        self.fills: list[Fill] = []
        self._realized = ZERO

    @property
    def held(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), ZERO)

    @property
    def open_cost(self) -> Decimal:
        return sum((lot.quantity * lot.price for lot in self.lots), ZERO)

    @property
    def realized(self) -> Decimal:
        return self._realized

    def _close(self, trade: ClosedTrade) -> None:
        self.closed.append(trade)
        self._realized += trade.pnl

    def bid(self, mark: Decimal) -> Decimal:
        return mark * (Decimal(1) - self.half)

    def buy(
        self, at: datetime, index: int, mark: Decimal, dollars: Decimal, *, step: int = -1
    ) -> None:
        price = mark * (Decimal(1) + self.half)
        if dollars <= ZERO or price <= ZERO:
            return
        self.lots.append(Lot(dollars / price, price, at, index, mark, step))
        self.fills.append(Fill(at, Side.BUY, dollars / price, price))
        self.buys += 1
        self.max_capital = max(self.max_capital, self.open_cost)

    def sell(self, at: datetime, mark: Decimal, quantity: Decimal) -> None:
        """Close ``quantity`` against the oldest lots, at the bid."""
        price = self.bid(mark)
        remaining = min(quantity, self.held)
        if remaining <= ZERO:
            return
        self.sells += 1
        self.fills.append(Fill(at, Side.SELL, remaining, price))
        while remaining > ZERO and self.lots:
            lot = self.lots[0]
            take = min(lot.quantity, remaining)
            self._close(ClosedTrade(lot.opened_at, at, take, lot.price, price))
            remaining -= take
            if take >= lot.quantity:
                self.lots.pop(0)
            else:
                lot.quantity -= take

    def sell_lot(self, at: datetime, mark: Decimal, lot: Lot) -> None:
        """Close one specific lot entirely, at the bid."""
        if lot not in self.lots:
            return
        self.sells += 1
        self.fills.append(Fill(at, Side.SELL, lot.quantity, self.bid(mark)))
        self._close(ClosedTrade(lot.opened_at, at, lot.quantity, lot.price, self.bid(mark)))
        self.lots.remove(lot)

    def unrealized(self, mark: Decimal) -> Decimal:
        bid = self.bid(mark)
        return sum((lot.quantity * (bid - lot.price) for lot in self.lots), ZERO)

    def mark(self, at: datetime, mark: Decimal) -> None:
        equity = self._realized + self.unrealized(mark)
        self.equity_curve.append((at, equity))
        self.peak_equity = max(self.peak_equity, equity)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - equity)


@dataclass
class Result:
    """One strategy on one symbol's bars."""

    strategy: str
    symbol: str
    bars: int
    start: datetime | None
    end: datetime | None
    round_trip_pct: Decimal
    buys: int = 0
    sells: int = 0
    closed: list[ClosedTrade] = field(default_factory=list)
    open_lots: list[Lot] = field(default_factory=list)
    realized: Decimal = ZERO
    unrealized: Decimal = ZERO
    max_capital: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    final_bid: Decimal = ZERO
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized

    @property
    def return_on_capital_pct(self) -> Decimal | None:
        if self.max_capital <= ZERO:
            return None
        return self.total / self.max_capital * Decimal(100)

    @property
    def closed_win_rate(self) -> float | None:
        """Closed trades that made money after the round trip."""
        if not self.closed:
            return None
        return sum(1 for t in self.closed if t.pnl > ZERO) / len(self.closed)

    @property
    def win_rate_with_open(self) -> float | None:
        """The same, with every still-open lot counted at the bid."""
        count = len(self.closed) + len(self.open_lots)
        if count == 0:
            return None
        wins = sum(1 for t in self.closed if t.pnl > ZERO)
        wins += sum(1 for lot in self.open_lots if self.final_bid > lot.price)
        return wins / count

    @property
    def average_win_pct(self) -> Decimal | None:
        wins = [t.return_pct for t in self.closed if t.pnl > ZERO]
        return sum(wins, ZERO) / len(wins) if wins else None

    @property
    def average_loss_pct(self) -> Decimal | None:
        losses = [t.return_pct for t in self.closed if t.pnl <= ZERO]
        return sum(losses, ZERO) / len(losses) if losses else None


def _finish(strategy: str, candles: Sequence[Candle], book: Book, round_trip: Decimal) -> Result:
    last = candles[-1].close if candles else ZERO
    return Result(
        strategy=strategy,
        symbol=canonical(candles[0].symbol) if candles else "",
        bars=len(candles),
        start=candles[0].start if candles else None,
        end=candles[-1].end if candles else None,
        round_trip_pct=round_trip,
        buys=book.buys,
        sells=book.sells,
        closed=list(book.closed),
        open_lots=list(book.lots),
        realized=book.realized,
        unrealized=book.unrealized(last),
        max_capital=book.max_capital,
        max_drawdown=book.max_drawdown,
        final_bid=book.bid(last),
        equity_curve=list(book.equity_curve),
        fills=list(book.fills),
    )


def ladder_name(mode: str, trend_days: int = 0, trend_exit: bool = False) -> str:
    """``ladder (anchor)``, ``ladder (anchor) +50d``, ``ladder (anchor) +50d exit``."""
    name = f"ladder ({mode})"
    if trend_days:
        name += f" +{trend_days}d"
        if trend_exit:
            name += " exit"
    return name


def live_strategy_name(config: "AgentConfig") -> str:
    """The row that replays what the agent trades, per config/strategy.yaml."""
    settings = config.strategy
    return ladder_name(MODE_ANCHOR, settings.trend_days, settings.ladder().trend_exit)


def run_ladder(
    candles: Sequence[Candle],
    *,
    steps: Sequence[tuple[Decimal, Decimal]] = DEFAULT_LADDER,
    mode: str = MODE_LOT,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    trend: Mapping[datetime, bool] | None = None,
    trend_exit: bool = False,
) -> Result:
    """The ladder, one :meth:`Ladder.decide` per bar, filled at the close.

    ``trend`` (from :func:`above_trend`) turns the filter on: no buy on a bar
    that did not close above its average. ``trend_exit`` also sells
    everything on a bar that closed at or under it. ``None`` is no filter.
    """
    rule = Ladder(
        steps=tuple(sorted(steps)),
        mode=mode,
        trend_filter=trend is not None,
        trend_exit=trend_exit,
    )
    book = Book(round_trip_pct / Decimal(2))
    anchor: Decimal | None = None
    bought: set[int] = set()
    sold: set[int] = set()
    in_cycle = False

    for index, candle in enumerate(candles):
        close = candle.close
        if book.held <= ZERO:
            if in_cycle:  # a sell emptied the position: start over from here
                bought, sold, in_cycle, anchor = set(), set(), False, close
            anchor = close if anchor is None else max(anchor, close)
        assert anchor is not None

        lots = (
            [HeldLot(lot.step, lot.mark, key=lot) for lot in book.lots]
            if mode == MODE_LOT
            else ()
        )
        orders = rule.decide(
            anchor=anchor,
            close=close,
            above=trend_lookup(trend, candle),
            held=book.held,
            bought=bought,
            sold=sold,
            lots=lots,
        )
        for order in orders:
            if order.side is Side.BUY:
                assert order.dollars is not None and order.step is not None
                book.buy(candle.end, index, close, order.dollars, step=order.step)
                bought.add(order.step)
                in_cycle = True
            elif isinstance(order.lot, Lot):
                book.sell_lot(candle.end, close, order.lot)
                bought.discard(order.lot.step)  # the step re-arms
            elif book.held > ZERO:
                quantity = (
                    book.held
                    if order.everything or order.dollars is None
                    else order.dollars / book.bid(close)
                )
                book.sell(candle.end, close, quantity)
                if order.step is not None:
                    sold.add(order.step)

        book.mark(candle.end, close)

    return _finish(ladder_name(mode), candles, book, round_trip_pct)


def run_trend(
    candles: Sequence[Candle],
    *,
    trend: Mapping[datetime, bool],
    dollars: Decimal = HOLD_DOLLARS,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    name: str = "trend",
) -> Result:
    """Hold ``dollars`` while the close is above its average, nothing below."""
    book = Book(round_trip_pct / Decimal(2))
    for index, candle in enumerate(candles):
        above = trend.get(candle.start)
        if book.held > ZERO and above is False:
            book.sell(candle.end, candle.close, book.held)
        elif book.held <= ZERO and above is True:
            book.buy(candle.end, index, candle.close, dollars)
        book.mark(candle.end, candle.close)
    return _finish(name, candles, book, round_trip_pct)


def run_hold(
    candles: Sequence[Candle],
    *,
    dollars: Decimal = HOLD_DOLLARS,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
) -> Result:
    book = Book(round_trip_pct / Decimal(2))
    for index, candle in enumerate(candles):
        if index == 0:
            book.buy(candle.end, index, candle.close, dollars)
        book.mark(candle.end, candle.close)
    return _finish("hold", candles, book, round_trip_pct)


def combined_drawdown(results: Sequence[Result]) -> Decimal:
    """Worst peak-to-trough of the summed equity across symbols."""
    latest: dict[str, Decimal] = {}
    points = sorted(
        ((at, r.symbol, equity) for r in results for at, equity in r.equity_curve),
        key=lambda p: p[0],
    )
    peak = drawdown = ZERO
    for _, symbol, equity in points:
        latest[symbol] = equity
        total = sum(latest.values(), ZERO)
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    return drawdown


def money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    rounded = round_money(value)
    return f"-${-rounded}" if rounded < 0 else f"${rounded}"


def pct(value: Decimal | float | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.1f}%"


def rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


STRATEGY_NAMES = ("ladder", "trend", "hold")
#: The stress run charges this much more spread than the base run.
STRESS_FACTOR = Decimal("1.5")


def run_all(
    candles: Sequence[Candle],
    *,
    strategies: Sequence[str] = STRATEGY_NAMES,
    steps: Sequence[tuple[Decimal, Decimal]] = DEFAULT_LADDER,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    trend: Mapping[datetime, bool] | None = None,
    trend_days: int = 0,
) -> list[Result]:
    """Every requested strategy on one symbol's bars.

    The ladder runs in both modes; with ``trend`` it runs again with the
    filter, and again with the filter and the exit. ``trend`` needs a trend
    map, so without one it is skipped.
    """
    results: list[Result] = []
    if "ladder" in strategies:
        for mode in LADDER_MODES:
            results.append(
                run_ladder(candles, steps=steps, mode=mode, round_trip_pct=round_trip_pct)
            )
        if trend is not None:
            for trend_exit in (False, True):
                for mode in LADDER_MODES:
                    filtered = run_ladder(
                        candles,
                        steps=steps,
                        mode=mode,
                        round_trip_pct=round_trip_pct,
                        trend=trend,
                        trend_exit=trend_exit,
                    )
                    results.append(
                        replace(filtered, strategy=ladder_name(mode, trend_days, trend_exit))
                    )
    if "trend" in strategies and trend is not None:
        results.append(
            run_trend(
                candles, trend=trend, round_trip_pct=round_trip_pct, name=f"trend +{trend_days}d"
            )
        )
    if "hold" in strategies:
        results.append(run_hold(candles, round_trip_pct=round_trip_pct))
    return results


@dataclass
class Pooled:
    """One strategy summed across symbols."""

    strategy: str
    results: list[Result]

    @property
    def closed(self) -> list[ClosedTrade]:
        return [t for r in self.results for t in r.closed]

    @property
    def trades(self) -> int:
        return sum(r.buys + r.sells for r in self.results)

    @property
    def total(self) -> Decimal:
        return sum((r.total for r in self.results), ZERO)

    @property
    def unrealized(self) -> Decimal:
        return sum((r.unrealized for r in self.results), ZERO)

    @property
    def max_capital(self) -> Decimal:
        """Summed per symbol: the most that could have been tied up at once."""
        return sum((r.max_capital for r in self.results), ZERO)

    @property
    def max_drawdown(self) -> Decimal:
        return combined_drawdown(self.results)

    @property
    def closed_win_rate(self) -> float | None:
        closed = self.closed
        return sum(1 for t in closed if t.pnl > ZERO) / len(closed) if closed else None

    @property
    def win_rate_with_open(self) -> float | None:
        count = sum(len(r.closed) + len(r.open_lots) for r in self.results)
        if count == 0:
            return None
        wins = sum(1 for t in self.closed if t.pnl > ZERO)
        wins += sum(1 for r in self.results for lot in r.open_lots if r.final_bid > lot.price)
        return wins / count


def pool(results: Sequence[Result]) -> list[Pooled]:
    names = list(dict.fromkeys(r.strategy for r in results))
    return [Pooled(name, [r for r in results if r.strategy == name]) for name in names]


NAME_WIDTH = 28

_HEADER = (
    f"  {'strategy':<{NAME_WIDTH}}{'trades':>7}{'win closed':>12}{'win +open':>11}"
    f"{'total P&L':>11}{'on capital':>12}{'worst dd':>10}{'tied up':>9}"
    f"{'open P&L':>10}{'stressed':>11}"
)


def _trades(row: Result | Pooled) -> int:
    return row.trades if isinstance(row, Pooled) else row.buys + row.sells


def _row(name: str, row: Result | Pooled, stressed: Result | Pooled) -> str:
    on_capital = row.total / row.max_capital * Decimal(100) if row.max_capital > ZERO else None
    return (
        f"  {name:<{NAME_WIDTH}}{_trades(row):>7}"
        f"{rate(row.closed_win_rate):>12}{rate(row.win_rate_with_open):>11}"
        f"{money(row.total):>11}{pct(on_capital):>12}{money(row.max_drawdown):>10}"
        f"{money(row.max_capital):>9}{money(row.unrealized):>10}{money(stressed.total):>11}"
    )


def _legend(
    steps: Sequence[tuple[Decimal, Decimal]], trend_days: int, config: "AgentConfig", names: set
) -> list[str]:
    total = sum((dollars for _, dollars in steps), ZERO)
    lines = [
        f"Ladder steps {describe_steps(steps)}: at most {money(total)} in a coin at once.",
    ]
    if trend_days:
        lines += [
            f"'+{trend_days}d' rows buy only on a bar that closed above its {trend_days}-day "
            "average; sells are unchanged.",
            f"'+{trend_days}d exit' rows also sell everything on a bar that closed at or under it.",
            f"'trend +{trend_days}d' holds {money(HOLD_DOLLARS)} while the close is above that "
            "average, and nothing at or under it.",
        ]
    lines.append(
        f"'hold' buys {money(HOLD_DOLLARS)} on the first bar. Sizes differ: compare 'on capital'."
    )
    lines.append(
        "The agent no longer trades the ladder: since 2026-09-28 it trades the split "
        "(--strategies split). These rows are the retired rule, for comparison."
    )
    return lines


def render_report(
    base: dict[str, list[Result]],
    stressed: dict[str, list[Result]],
    *,
    steps: Sequence[tuple[Decimal, Decimal]],
    round_trip_pct: Decimal,
    config: "AgentConfig",
    trend_days: int = 0,
) -> str:
    """The comparison, per symbol and pooled, with what to read first."""
    everything = [r for results in base.values() for r in results]
    starts = [r.start for r in everything if r.start]
    ends = [r.end for r in everything if r.end]
    lines = [
        "=" * 110,
        "BACKTEST",
        "=" * 110,
        f"{min(starts):%Y-%m-%d} to {max(ends):%Y-%m-%d}, "
        f"{config.strategy.bar_interval_minutes}-minute bars, trading at bar closes."
        if starts and ends
        else "no bars",
        f"Every trade crosses a {round_trip_pct}% round trip, half on each side; "
        f"'stressed' re-runs at {round_trip_pct * STRESS_FACTOR}%.",
        *_legend(steps, trend_days, config, {r.strategy for r in everything}),
        "",
        "Read total P&L, worst drawdown and 'win +open' first. 'win closed' ignores",
        "positions never sold, which is where a ladder keeps its losses.",
    ]
    for symbol, results in base.items():
        lines += ["", f"{symbol} ({results[0].bars} bars)" if results else symbol, _HEADER]
        by_name = {r.strategy: r for r in stressed.get(symbol, [])}
        for result in results:
            lines.append(_row(result.strategy, result, by_name[result.strategy]))
    if len(base) > 1:
        stressed_pool = {p.strategy: p for p in pool([r for rs in stressed.values() for r in rs])}
        lines += ["", f"ALL {len(base)} SYMBOLS (drawdown on the combined equity)", _HEADER]
        for pooled in pool(everything):
            lines.append(_row(pooled.strategy, pooled, stressed_pool[pooled.strategy]))
    lines += [
        "",
        "trades = orders placed. 'tied up' = the most money held in open positions",
        "at once; 'on capital' = total P&L over that. A backtest is evidence about",
        "a rule on past prices, not about the next trade: it authorizes nothing.",
    ]
    return "\n".join(lines)


# -- rolling windows --------------------------------------------------------


def window_starts(
    series: Mapping[str, Sequence[Candle]], *, window: timedelta, step: timedelta
) -> list[datetime]:
    """Window starts, the last window ending on the last bar every symbol has.

    Only whole windows count: one that would start before a symbol's first bar
    is dropped, so every window compares the same span for every symbol.
    """
    if window <= timedelta(0) or step <= timedelta(0):
        raise ValueError("the window and the step must be positive")
    firsts = [bars[0].start for bars in series.values() if bars]
    lasts = [bars[-1].end for bars in series.values() if bars]
    if not firsts:
        return []
    first, end = max(firsts), min(lasts)
    starts = []
    start = end - window
    while start >= first:
        starts.append(start)
        start -= step
    return sorted(starts)


@dataclass
class RollingWindow:
    start: datetime
    end: datetime
    base: dict[str, list[Result]]
    stressed: dict[str, list[Result]]


def run_rolling(
    series: Mapping[str, Sequence[Candle]],
    trends: Mapping[str, Mapping[datetime, bool] | None],
    *,
    window: timedelta,
    step: timedelta,
    strategies: Sequence[str] = STRATEGY_NAMES,
    steps: Sequence[tuple[Decimal, Decimal]] = DEFAULT_LADDER,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    trend_days: int = 0,
) -> list[RollingWindow]:
    """Every strategy over every window, each window starting flat.

    The trend maps cover the whole history, so a window's average is already
    warm on its first bar.
    """
    windows = []
    for start in window_starts(series, window=window, step=step):
        end = start + window
        base: dict[str, list[Result]] = {}
        stressed: dict[str, list[Result]] = {}
        for symbol, bars in series.items():
            candles = [c for c in bars if start <= c.start and c.end <= end]
            if not candles:
                continue
            for runs, cost in ((base, round_trip_pct), (stressed, round_trip_pct * STRESS_FACTOR)):
                runs[symbol] = run_all(
                    candles,
                    strategies=strategies,
                    steps=steps,
                    round_trip_pct=cost,
                    trend=trends.get(symbol),
                    trend_days=trend_days,
                )
        windows.append(RollingWindow(start, end, base, stressed))
    return windows


def _on_capital(total: Decimal, capital: Decimal) -> float:
    """Return on capital in percent; a window that never traded made 0%."""
    return float(total / capital * Decimal(100)) if capital > ZERO else 0.0


@dataclass
class RollingRow:
    """One strategy across every window."""

    strategy: str
    returns: list[float] = field(default_factory=list)
    hold_returns: list[float] = field(default_factory=list)
    stressed_returns: list[float] = field(default_factory=list)
    stressed_hold_returns: list[float] = field(default_factory=list)

    @property
    def windows(self) -> int:
        return len(self.returns)

    @property
    def positive(self) -> int:
        return sum(1 for r in self.returns if r > 0)

    @property
    def beat_hold(self) -> int | None:
        if not self.hold_returns:
            return None
        return sum(1 for r, h in zip(self.returns, self.hold_returns, strict=True) if r > h)

    @property
    def beat_hold_stressed(self) -> int | None:
        if not self.stressed_hold_returns:
            return None
        return sum(
            1
            for r, h in zip(self.stressed_returns, self.stressed_hold_returns, strict=True)
            if r > h
        )


def rolling_rows(
    windows: Sequence[RollingWindow], *, symbol: str | None = None
) -> list[RollingRow]:
    """Per strategy: its return on capital in each window, beside hold's.

    ``symbol`` picks one symbol; ``None`` pools every symbol per window, as
    total P&L over total capital tied up.
    """
    rows: dict[str, RollingRow] = {}

    def returns(results: Mapping[str, list[Result]]) -> dict[str, float]:
        picked = [
            r
            for sym, rs in results.items()
            if symbol is None or sym == symbol
            for r in rs
        ]
        out = {}
        for pooled in pool(picked):
            out[pooled.strategy] = _on_capital(pooled.total, pooled.max_capital)
        return out

    for window in windows:
        base = returns(window.base)
        stressed = returns(window.stressed)
        if not base:
            continue
        hold, hold_stressed = base.get("hold"), stressed.get("hold")
        for name, value in base.items():
            row = rows.setdefault(name, RollingRow(name))
            row.returns.append(value)
            row.stressed_returns.append(stressed[name])
            if name != "hold" and hold is not None and hold_stressed is not None:
                row.hold_returns.append(hold)
                row.stressed_hold_returns.append(hold_stressed)
    return list(rows.values())


_ROLLING_HEADER = (
    f"  {'strategy':<{NAME_WIDTH}}{'windows':>8}{'positive':>10}{'beat hold':>11}"
    f"{'stressed':>10}{'median':>9}{'worst':>9}{'best':>9}"
)


def _count(value: int | None, total: int) -> str:
    return "-" if value is None else f"{value}/{total}"


def _rolling_line(row: RollingRow) -> str:
    return (
        f"  {row.strategy:<{NAME_WIDTH}}{row.windows:>8}"
        f"{_count(row.positive, row.windows):>10}"
        f"{_count(row.beat_hold, row.windows):>11}"
        f"{_count(row.beat_hold_stressed, row.windows):>10}"
        f"{pct(statistics.median(row.returns)):>9}"
        f"{pct(min(row.returns)):>9}{pct(max(row.returns)):>9}"
    )


def render_rolling(
    windows: Sequence[RollingWindow], *, window: timedelta, step: timedelta
) -> str:
    if not windows:
        return (
            f"ROLLING WINDOWS: no whole {window.days}-day window fits in the history. "
            "Fetch more (--days) or shorten --roll-window."
        )
    symbols = list(dict.fromkeys(s for w in windows for s in w.base))
    lines = [
        "=" * 110,
        "ROLLING WINDOWS",
        "=" * 110,
        f"{window.days}-day windows, a new one every {step.days} days: {len(windows)} "
        f"window{'' if len(windows) == 1 else 's'} "
        f"from {windows[0].start:%Y-%m-%d} to {windows[-1].end:%Y-%m-%d}.",
        "Each window starts flat. Every figure is return on capital ('on capital' above);",
        "a window with no trade counts as 0%. 'beat hold' compares with hold in the same",
        "window, and 'stressed' does the same at the stressed round trip.",
    ]
    for symbol in symbols:
        lines += ["", symbol, _ROLLING_HEADER]
        lines += [_rolling_line(row) for row in rolling_rows(windows, symbol=symbol)]
    if len(symbols) > 1:
        lines += ["", f"ALL {len(symbols)} SYMBOLS (pooled per window)", _ROLLING_HEADER]
        lines += [_rolling_line(row) for row in rolling_rows(windows)]
    return "\n".join(lines)


def summary(result: Result) -> dict[str, object]:
    """One result's numbers, JSON-safe."""
    return {
        "strategy": result.strategy,
        "symbol": result.symbol,
        "bars": result.bars,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "round_trip_pct": str(result.round_trip_pct),
        "buys": result.buys,
        "sells": result.sells,
        "closed_trades": len(result.closed),
        "open_lots": len(result.open_lots),
        "closed_win_rate": result.closed_win_rate,
        "win_rate_with_open": result.win_rate_with_open,
        "average_win_pct": str(round_money(result.average_win_pct, 3))
        if result.average_win_pct is not None
        else None,
        "average_loss_pct": str(round_money(result.average_loss_pct, 3))
        if result.average_loss_pct is not None
        else None,
        "realized": str(round_money(result.realized)),
        "unrealized": str(round_money(result.unrealized)),
        "total": str(round_money(result.total)),
        "max_capital": str(round_money(result.max_capital)),
        "max_drawdown": str(round_money(result.max_drawdown)),
    }
