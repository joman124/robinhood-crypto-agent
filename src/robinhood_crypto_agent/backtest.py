"""Backtests over hourly bars, under the round trip Robinhood actually charges.

Three strategies on the same bars:

``ladder``
    Buy the dip, sell the rip, in dollar steps. With the default steps (5:5,
    10:10, 20:20) it buys $5 when the close is 5% under the anchor, $10 at 10%
    under, $20 at 20% under. While nothing is held the anchor follows the price
    up, so a dip is measured from the high since the last cycle; once a sell
    empties the position the anchor resets to that close. "Up 5%" reads two
    ways, so there are two modes:

    * ``lot`` -- each buy sells when the price is up its own step from where
      *it* was bought: the $5 lot at +5%, the $10 lot at +10%, the $20 lot at
      +20%. A step re-arms once its lot is sold. This is a grid.
    * ``anchor`` -- both directions measure from the one anchor: sell $5 worth
      at 5% over it, $10 at 10% over, and everything left at 20% over. Each
      step fires once per cycle.

    With a trend filter (``--trend-days N``) a ladder buys only on a bar that
    closed above the average of the last N days of closes; below it, it buys
    nothing. Sells are unchanged, so a coin already held still exits by the
    ladder's own rules. The average is warmed up on N days of bars fetched
    before the window, so the window itself is the same as without the filter.

``signal``
    This agent's own System 1: the composite view each bar, buying only what
    would clear the risk floors *and* the escalation trigger, sized as live,
    and closed by the time exit (``exit_after_bars``) or by a strong bearish
    view on a held coin when sells are enabled. System 2 cannot be replayed
    and there is no news history, so this is System 1 alone.

``hold``
    Buy once on the first bar and hold: the baseline both have to beat.

Every trade crosses the spread: a buy pays the mark plus half of it, a sell
gets the mark less half, at bar closes. The report counts what a win rate
hides. Positions still open at the end are marked to the bid and shown
separately, and a second win rate counts them. The worst drawdown and the
most capital tied up are reported next to the profit. A ladder can close
nearly every trade at a profit while holding a large loss it never sold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from .config import AgentConfig
from .models import Candle, CompositeView, Direction, PairConstraints, Position, Side
from .numeric import ZERO, round_money
from .sizing import size_position
from .strategy import CompositeStrategy
from .symbols import canonical

#: (percent from the anchor, dollars) -- the steps the owner proposed.
DEFAULT_LADDER: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("5"), Decimal("5")),
    (Decimal("10"), Decimal("10")),
    (Decimal("20"), Decimal("20")),
)
#: The round trip measured on Robinhood's market-maker quotes, 2026-09.
DEFAULT_ROUND_TRIP_PCT = Decimal("1.9")
LADDER_MODES = ("lot", "anchor")
#: What the buy-and-hold baseline invests.
HOLD_DOLLARS = Decimal("100")
#: The account the signal strategy is sized against.
DEFAULT_PORTFOLIO = Decimal("500")
#: Bars of history the composite view reads, as ``rhca analyze`` does.
LOOKBACK_BARS = 200


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

    @property
    def held(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), ZERO)

    @property
    def open_cost(self) -> Decimal:
        return sum((lot.quantity * lot.price for lot in self.lots), ZERO)

    @property
    def realized(self) -> Decimal:
        return sum((trade.pnl for trade in self.closed), ZERO)

    def buy(
        self, at: datetime, index: int, mark: Decimal, dollars: Decimal, *, step: int = -1
    ) -> None:
        price = mark * (Decimal(1) + self.half)
        if dollars <= ZERO or price <= ZERO:
            return
        self.lots.append(Lot(dollars / price, price, at, index, mark, step))
        self.buys += 1
        self.max_capital = max(self.max_capital, self.open_cost)

    def sell(self, at: datetime, mark: Decimal, quantity: Decimal) -> None:
        """Close ``quantity`` against the oldest lots, at the bid."""
        price = mark * (Decimal(1) - self.half)
        remaining = min(quantity, self.held)
        if remaining <= ZERO:
            return
        self.sells += 1
        while remaining > ZERO and self.lots:
            lot = self.lots[0]
            take = min(lot.quantity, remaining)
            self.closed.append(ClosedTrade(lot.opened_at, at, take, lot.price, price))
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
        price = mark * (Decimal(1) - self.half)
        self.closed.append(ClosedTrade(lot.opened_at, at, lot.quantity, lot.price, price))
        self.lots.remove(lot)

    def sell_lots_opened_by(self, at: datetime, mark: Decimal, index: int) -> None:
        """The time exit: sell every lot opened at or before bar ``index``."""
        due = sum((lot.quantity for lot in self.lots if lot.opened_index <= index), ZERO)
        if due > ZERO:
            self.sell(at, mark, due)

    def unrealized(self, mark: Decimal) -> Decimal:
        bid = mark * (Decimal(1) - self.half)
        return sum((lot.quantity * (bid - lot.price) for lot in self.lots), ZERO)

    def mark(self, at: datetime, mark: Decimal) -> None:
        equity = self.realized + self.unrealized(mark)
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
        final_bid=last * (Decimal(1) - book.half),
        equity_curve=list(book.equity_curve),
    )


def above_trend(candles: Sequence[Candle], bars: int) -> dict[datetime, bool]:
    """Per bar start: did it close above the average of the last ``bars`` closes?

    The average includes the bar itself, and a bar with fewer than ``bars``
    closes behind it has no entry -- the filter reads that as "not above", so
    nothing is bought before the average exists.
    """
    if bars <= 0:
        raise ValueError("the trend average needs at least one bar")
    out: dict[datetime, bool] = {}
    closes: list[Decimal] = []
    total = ZERO
    for candle in candles:
        closes.append(candle.close)
        total += candle.close
        if len(closes) > bars:
            total -= closes[-bars - 1]
        if len(closes) >= bars:
            out[candle.start] = candle.close > total / Decimal(bars)
    return out


def trend_bars(days: int, bar_interval_minutes: int) -> int:
    """How many bars make ``days`` of history."""
    return max(1, days * 24 * 60 // bar_interval_minutes)


def run_ladder(
    candles: Sequence[Candle],
    *,
    steps: Sequence[tuple[Decimal, Decimal]] = DEFAULT_LADDER,
    mode: str = "lot",
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    trend: Mapping[datetime, bool] | None = None,
) -> Result:
    """The dip/rip ladder; see the module docstring for the exact rules.

    ``trend`` (from :func:`above_trend`) blocks every buy on a bar that did not
    close above its average. ``None`` is no filter.
    """
    if mode not in LADDER_MODES:
        raise ValueError(f"ladder mode must be one of {LADDER_MODES}, got {mode!r}")
    book = Book(round_trip_pct / Decimal(2))
    steps = sorted(steps)
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

        if mode == "lot":
            for lot in list(book.lots):
                pct = steps[lot.step][0]
                if close >= lot.mark * (Decimal(1) + pct / Decimal(100)):
                    book.sell_lot(candle.end, close, lot)
                    bought.discard(lot.step)  # the step re-arms
        else:
            for level, (pct, dollars) in enumerate(steps):
                if level in sold or book.held <= ZERO:
                    continue
                if close >= anchor * (Decimal(1) + pct / Decimal(100)):
                    bid = close * (Decimal(1) - book.half)
                    last_step = level == len(steps) - 1
                    book.sell(candle.end, close, book.held if last_step else dollars / bid)
                    sold.add(level)

        buying = trend is None or trend.get(candle.start, False)
        for level, (pct, dollars) in enumerate(steps):
            if level in bought or not buying:
                continue
            if close <= anchor * (Decimal(1) - pct / Decimal(100)):
                book.buy(candle.end, index, close, dollars, step=level)
                bought.add(level)
                in_cycle = True

        book.mark(candle.end, close)

    return _finish(f"ladder ({mode})", candles, book, round_trip_pct)


def signal_views(candles: Sequence[Candle], config: AgentConfig) -> list[CompositeView | None]:
    """The composite view at every bar close, or ``None`` before ``min_bars``.

    Separate from the trading loop because it is most of the cost and does not
    depend on the spread: the base and stressed runs share one set.
    """
    strategy = CompositeStrategy(config.strategy)
    symbol = canonical(candles[0].symbol) if candles else ""
    views: list[CompositeView | None] = []
    for index in range(len(candles)):
        if index + 1 < config.strategy.min_bars:
            views.append(None)
            continue
        window = candles[max(0, index + 1 - LOOKBACK_BARS) : index + 1]
        views.append(strategy.evaluate(symbol, window))
    return views


def run_signal(
    candles: Sequence[Candle],
    config: AgentConfig,
    *,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    portfolio_value: Decimal = DEFAULT_PORTFOLIO,
    views: Sequence[CompositeView | None] | None = None,
) -> Result:
    """System 1 as ``rhca run`` trades it, less System 2 and news.

    Buys must clear the risk floors, the escalation trigger, the cooldown, the
    concentration cap on ``portfolio_value`` and the daily notional cap.
    """
    book = Book(round_trip_pct / Decimal(2))
    if views is None:
        views = signal_views(candles, config)
    limits, pipeline, settings = config.risk, config.pipeline, config.strategy
    min_score = max(limits.min_abs_score, pipeline.trigger_min_abs_score)
    min_confidence = max(limits.min_signal_confidence, pipeline.trigger_min_confidence)
    cooldown = max(1, math.ceil(pipeline.escalation_cooldown_minutes / settings.bar_interval_minutes))
    symbol = canonical(candles[0].symbol) if candles else ""
    constraints = PairConstraints.permissive(symbol)
    position_cap = portfolio_value * limits.max_position_pct_of_portfolio / Decimal(100)
    last_buy: int | None = None
    traded: dict[object, Decimal] = {}  # notional per UTC day, for the daily cap

    for index, candle in enumerate(candles):
        close = candle.close
        if settings.exit_after_bars > 0:
            book.sell_lots_opened_by(candle.end, close, index - settings.exit_after_bars)

        view = views[index]
        if view is not None:
            window = candles[max(0, index + 1 - LOOKBACK_BARS) : index + 1]
            strong = (
                Decimal(str(abs(view.score))) >= min_score
                and Decimal(str(view.confidence)) >= min_confidence
            )
            if strong and view.direction is Direction.LONG and (
                last_buy is None or index - last_buy >= cooldown
            ):
                held = book.held
                sizing = size_position(
                    view,
                    reference_price=close * (Decimal(1) + book.half),
                    candles=window,
                    limits=limits,
                    strategy=settings,
                    constraints=constraints,
                    portfolio_value=portfolio_value,
                    position=Position(symbol, held) if held > ZERO else None,
                )
                day = candle.end.date()
                # The concentration and daily-notional rules, as the risk
                # engine applies them live.
                within_caps = (
                    held * close + sizing.notional <= position_cap
                    and traded.get(day, ZERO) + sizing.notional <= limits.max_daily_notional_usd
                )
                if sizing.viable and sizing.side is Side.BUY and within_caps:
                    book.buy(candle.end, index, close, sizing.notional)
                    traded[day] = traded.get(day, ZERO) + sizing.notional
                    last_buy = index
            elif (
                strong
                and view.direction is Direction.SHORT
                and book.held > ZERO
                and not limits.disable_sell_side
            ):
                book.sell(candle.end, close, book.held)

        book.mark(candle.end, close)

    return _finish("signal", candles, book, round_trip_pct)


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


def parse_ladder(text: str) -> tuple[tuple[Decimal, Decimal], ...]:
    """``"5:5,10:10,20:20"`` -> ((5, 5), (10, 10), (20, 20))."""
    steps = []
    for part in text.split(","):
        pct, _, dollars = part.strip().partition(":")
        step = (Decimal(pct), Decimal(dollars))
        if step[0] <= ZERO or step[1] <= ZERO:
            raise ValueError(f"ladder step {part!r} must be positive percent:dollars")
        steps.append(step)
    if not steps:
        raise ValueError("the ladder needs at least one step")
    return tuple(sorted(steps))


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


STRATEGY_NAMES = ("ladder", "signal", "hold")
#: The stress run charges this much more spread than the base run.
STRESS_FACTOR = Decimal("1.5")


def run_all(
    candles: Sequence[Candle],
    config: AgentConfig,
    *,
    strategies: Sequence[str] = STRATEGY_NAMES,
    steps: Sequence[tuple[Decimal, Decimal]] = DEFAULT_LADDER,
    round_trip_pct: Decimal = DEFAULT_ROUND_TRIP_PCT,
    views: Sequence[CompositeView | None] | None = None,
    trend: Mapping[datetime, bool] | None = None,
    trend_days: int = 0,
) -> list[Result]:
    """Every requested strategy on one symbol's bars; the ladder in both modes,
    and again under the trend filter when ``trend`` is given."""
    results: list[Result] = []
    if "ladder" in strategies:
        for mode in LADDER_MODES:
            results.append(
                run_ladder(candles, steps=steps, mode=mode, round_trip_pct=round_trip_pct)
            )
        if trend is not None:
            for mode in LADDER_MODES:
                filtered = run_ladder(
                    candles, steps=steps, mode=mode, round_trip_pct=round_trip_pct, trend=trend
                )
                results.append(replace(filtered, strategy=f"{filtered.strategy} +{trend_days}d"))
    if "signal" in strategies:
        results.append(run_signal(candles, config, round_trip_pct=round_trip_pct, views=views))
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


_HEADER = (
    f"  {'strategy':<22}{'trades':>7}{'win closed':>12}{'win +open':>11}"
    f"{'total P&L':>11}{'on capital':>12}{'worst dd':>10}{'tied up':>9}"
    f"{'open P&L':>10}{'stressed':>11}"
)


def _trades(row: Result | Pooled) -> int:
    return row.trades if isinstance(row, Pooled) else row.buys + row.sells


def _row(name: str, row: Result | Pooled, stressed: Result | Pooled) -> str:
    on_capital = row.total / row.max_capital * Decimal(100) if row.max_capital > ZERO else None
    return (
        f"  {name:<22}{_trades(row):>7}"
        f"{rate(row.closed_win_rate):>12}{rate(row.win_rate_with_open):>11}"
        f"{money(row.total):>11}{pct(on_capital):>12}{money(row.max_drawdown):>10}"
        f"{money(row.max_capital):>9}{money(row.unrealized):>10}{money(stressed.total):>11}"
    )


def render_report(
    base: dict[str, list[Result]],
    stressed: dict[str, list[Result]],
    *,
    steps: Sequence[tuple[Decimal, Decimal]],
    round_trip_pct: Decimal,
    config: AgentConfig,
    trend_days: int = 0,
) -> str:
    """The comparison, per symbol and pooled, with what to read first."""
    everything = [r for results in base.values() for r in results]
    starts = [r.start for r in everything if r.start]
    ends = [r.end for r in everything if r.end]
    ladder = ", ".join(f"-{p}%/${d}" for p, d in steps)
    lines = [
        "=" * 100,
        "BACKTEST",
        "=" * 100,
        f"{min(starts):%Y-%m-%d} to {max(ends):%Y-%m-%d}, "
        f"{config.strategy.bar_interval_minutes}-minute bars, trading at bar closes."
        if starts and ends
        else "no bars",
        f"Every trade crosses a {round_trip_pct}% round trip, half on each side; "
        f"'stressed' re-runs at {round_trip_pct * STRESS_FACTOR}%.",
        f"Ladder steps {ladder}. Signal = System 1 with the "
        f"{config.strategy.exit_after_bars}-bar exit; no System 2, no news.",
        *(
            [
                f"'+{trend_days}d' rows: the same ladders, buying only on a bar that closed "
                f"above its {trend_days}-day average. Sells are unchanged."
            ]
            if trend_days
            else []
        ),
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
