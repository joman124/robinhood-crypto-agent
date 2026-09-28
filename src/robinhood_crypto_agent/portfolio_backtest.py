"""The breakout, backtested as one account across several coins.

The ladder's backtest runs each coin on its own fixed dollars. The breakout
sizes each trade from the account's equity, so it has to be run as one
account: exits free cash, entries size off what the account is worth that
day, and the result is the account's return and drawdown.

It is judged against **equal-weight hold** of the same coins, with the same
money, under the same spread. Every fill crosses half the round trip at the
day's close, as in the ladder's backtest.

Two numbers answer whether it has an edge, whatever the position size:

- **Expectancy in R.** R is what a trade would lose at its initial stop. A
  trade that makes 2R made twice what it risked. Average R over closed trades,
  after costs, above zero is an edge; the account's return is that edge times
  how much is risked.
- **Return over max drawdown**, against hold's. Raw return rewards whoever
  held the most coin through a rally.

The **split** runs one account as two sleeves that never pass cash between
them: a long-term one that buys and holds (``strategy/hodl.py``) and a
short-term one that trades the breakout, sized off its own sleeve.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .backtest import STRESS_FACTOR, money, pct, window_starts
from .models import Candle
from .numeric import ZERO
from .strategy.breakout import Breakout, DayView, daily_bars, day_views
from .strategy.hodl import DIP, LUMP, MODES, Accumulate, moving_averages

DEFAULT_CAPITAL = Decimal("500")
DEFAULT_MIN_TRADE = Decimal("5")
ONE_HUNDRED = Decimal("100")


@dataclass
class Prepared:
    """One coin's daily bars and indicators, computed once per run."""

    daily: list[Candle]
    views: dict[datetime, DayView]


def prepare(series: Mapping[str, Sequence[Candle]], rule: Breakout) -> dict[str, Prepared]:
    prepared = {}
    for symbol, hourly in series.items():
        daily = daily_bars(hourly)
        prepared[symbol] = Prepared(daily, day_views(daily, rule))
    return prepared


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_day: datetime
    exit_day: datetime | None
    quantity: Decimal
    #: Dollars paid, spread included.
    cost: Decimal
    #: Dollars received, spread included -- or the position's worth at the
    #: bid on the last day, for a trade still open.
    proceeds: Decimal
    #: Dollars it would have lost at its initial stop: one R.
    risk: Decimal

    @property
    def open(self) -> bool:
        return self.exit_day is None

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def r(self) -> Decimal | None:
        return self.pnl / self.risk if self.risk > ZERO else None


@dataclass
class Position:
    symbol: str
    quantity: Decimal
    entry_day: datetime
    cost: Decimal
    risk: Decimal
    highest: Decimal


@dataclass
class PortfolioResult:
    name: str
    symbols: list[str]
    capital: Decimal
    round_trip_pct: Decimal
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    #: Share of equity in coins at each day's close.
    exposure: list[Decimal] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        return self.equity_curve[0][0] if self.equity_curve else None

    @property
    def end(self) -> datetime | None:
        return self.equity_curve[-1][0] if self.equity_curve else None

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1][1] if self.equity_curve else self.capital

    @property
    def total_return_pct(self) -> Decimal:
        return (self.final_equity / self.capital - 1) * ONE_HUNDRED

    @property
    def max_drawdown_pct(self) -> Decimal:
        peak = self.capital
        worst = ZERO
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > ZERO:
                worst = max(worst, (peak - equity) / peak * ONE_HUNDRED)
        return worst

    @property
    def return_over_drawdown(self) -> Decimal | None:
        drawdown = self.max_drawdown_pct
        return self.total_return_pct / drawdown if drawdown > ZERO else None

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if not t.open]

    @property
    def win_rate(self) -> float | None:
        closed = self.closed
        return sum(1 for t in closed if t.pnl > ZERO) / len(closed) if closed else None

    def _mean_r(self, trades: Sequence[Trade]) -> Decimal | None:
        rs = [t.r for t in trades if t.r is not None]
        return sum(rs, ZERO) / Decimal(len(rs)) if rs else None

    @property
    def expectancy_r(self) -> Decimal | None:
        """Mean R over closed trades, after costs."""
        return self._mean_r(self.closed)

    @property
    def average_win_r(self) -> Decimal | None:
        return self._mean_r([t for t in self.closed if t.pnl > ZERO])

    @property
    def average_loss_r(self) -> Decimal | None:
        return self._mean_r([t for t in self.closed if t.pnl <= ZERO])

    @property
    def average_exposure_pct(self) -> Decimal:
        if not self.exposure:
            return ZERO
        return sum(self.exposure, ZERO) / Decimal(len(self.exposure)) * ONE_HUNDRED

    @property
    def final_exposure_pct(self) -> Decimal:
        """The share in coins on the last day: for a sleeve that only buys,
        what is left is cash it never spent."""
        return self.exposure[-1] * ONE_HUNDRED if self.exposure else ZERO

    def pnl_by_symbol(self) -> dict[str, Decimal]:
        totals = dict.fromkeys(self.symbols, ZERO)
        for trade in self.trades:
            totals[trade.symbol] = totals.get(trade.symbol, ZERO) + trade.pnl
        return totals


def _days(prepared: Mapping[str, Prepared], start: datetime | None, end: datetime | None):
    days = sorted(
        {
            bar.start
            for p in prepared.values()
            for bar in p.daily
            if (start is None or bar.start >= start) and (end is None or bar.end <= end)
        }
    )
    closes = {
        symbol: {bar.start: bar.close for bar in p.daily} for symbol, p in prepared.items()
    }
    return days, closes


def run_breakout(
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    name: str = "breakout",
) -> PortfolioResult:
    """Trade the breakout on every coin in ``prepared``, from one account.

    Only days inside ``[start, end)`` are traded; the indicators read every
    bar before them, so a window starts with its averages already warm.
    """
    half = round_trip_pct / Decimal(200)
    symbols = sorted(prepared)
    result = PortfolioResult(name, symbols, capital, round_trip_pct)
    days, closes = _days(prepared, start, end)
    cash = capital
    positions: dict[str, Position] = {}
    last_close: dict[str, Decimal] = {}

    def equity() -> Decimal:
        held = sum(
            (p.quantity * last_close[s] * (1 - half) for s, p in positions.items()), ZERO
        )
        return cash + held

    for day in days:
        for symbol in symbols:
            if day in closes[symbol]:
                last_close[symbol] = closes[symbol][day]

        for symbol in sorted(positions):
            view = prepared[symbol].views.get(day)
            if view is None:
                continue
            position = positions[symbol]
            position.highest = max(position.highest, view.close)
            if rule.exits(position.highest, view):
                proceeds = position.quantity * view.close * (1 - half)
                cash += proceeds
                result.trades.append(
                    Trade(symbol, position.entry_day, day, position.quantity, position.cost,
                          proceeds, position.risk)
                )
                del positions[symbol]

        account = equity()
        for symbol in symbols:
            if symbol in positions:
                continue
            view = prepared[symbol].views.get(day)
            if view is None or not rule.enters(view):
                continue
            dollars = min(rule.position_dollars(account, view), cash)
            if dollars < min_trade:
                continue
            price = view.close * (1 + half)
            cash -= dollars
            positions[symbol] = Position(
                symbol=symbol,
                quantity=dollars / price,
                entry_day=day,
                cost=dollars,
                risk=dollars * rule.stop_fraction(view),
                highest=view.close,
            )

        account = equity()
        result.equity_curve.append((day, account))
        result.exposure.append((account - cash) / account if account > ZERO else ZERO)

    for symbol, position in sorted(positions.items()):
        proceeds = position.quantity * last_close[symbol] * (1 - half)
        result.trades.append(
            Trade(symbol, position.entry_day, None, position.quantity, position.cost,
                  proceeds, position.risk)
        )
    return result


def run_accumulate(
    prepared: Mapping[str, Prepared],
    plan: Accumulate,
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    name: str | None = None,
) -> PortfolioResult:
    """The long-term sleeve: each coin's even share bought in tranches, as
    ``plan`` says, and never sold. Every tranche is one open trade, marked at
    the bid on the last day."""
    half = round_trip_pct / Decimal(200)
    symbols = sorted(prepared)
    result = PortfolioResult(name or f"long-term: {plan.label}", symbols, capital, round_trip_pct)
    days, closes = _days(prepared, start, end)
    tranche = capital / Decimal(len(symbols)) / Decimal(plan.tranche_count) if symbols else ZERO
    averages = (
        {s: moving_averages(p.daily, plan.average_days) for s, p in prepared.items()}
        if plan.mode == DIP
        else {}
    )
    cash = capital
    held = dict.fromkeys(symbols, ZERO)
    bought = dict.fromkeys(symbols, 0)
    last_buy: dict[str, datetime] = {}
    last_close: dict[str, Decimal] = {}
    buys: list[tuple[str, datetime, Decimal, Decimal]] = []
    for day in days:
        for symbol in symbols:
            close = closes[symbol].get(day)
            if close is None:
                continue
            last_close[symbol] = close
            if bought[symbol] >= plan.tranche_count or not plan.due(
                day=day,
                close=close,
                average=averages.get(symbol, {}).get(day),
                last_buy=last_buy.get(symbol),
            ):
                continue
            dollars = min(tranche, cash)
            if dollars <= ZERO or dollars < min_trade:
                continue
            quantity = dollars / (close * (1 + half))
            cash -= dollars
            held[symbol] += quantity
            bought[symbol] += 1
            last_buy[symbol] = day
            buys.append((symbol, day, quantity, dollars))
        worth = sum(
            (q * last_close[s] * (1 - half) for s, q in held.items() if q > ZERO), ZERO
        )
        account = cash + worth
        result.equity_curve.append((day, account))
        result.exposure.append(worth / account if account > ZERO else ZERO)
    for symbol, day, quantity, dollars in sorted(buys, key=lambda b: (b[0], b[1])):
        proceeds = quantity * last_close[symbol] * (1 - half)
        result.trades.append(Trade(symbol, day, None, quantity, dollars, proceeds, ZERO))
    return result


def run_equal_hold(
    prepared: Mapping[str, Prepared],
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
) -> PortfolioResult:
    """The baseline: the same money split evenly across the coins on their
    first day in the window, and held."""
    return run_accumulate(
        prepared,
        Accumulate(mode=LUMP),
        capital=capital,
        round_trip_pct=round_trip_pct,
        start=start,
        end=end,
        min_trade=ZERO,
        name="hold (equal weight)",
    )


def combine(name: str, parts: Sequence[PortfolioResult]) -> PortfolioResult:
    """One account made of sleeves that keep their own cash: each day's
    equity is the sum of theirs, and its exposure their coins over that sum."""
    symbols = sorted({s for part in parts for s in part.symbols})
    trip = parts[0].round_trip_pct if parts else ZERO
    result = PortfolioResult(name, symbols, sum((p.capital for p in parts), ZERO), trip)
    curves = [
        {day: (equity, exposure) for (day, equity), exposure in zip(p.equity_curve, p.exposure, strict=True)}
        for p in parts
    ]
    latest = [(p.capital, ZERO) for p in parts]
    for day in sorted({day for p in parts for day, _ in p.equity_curve}):
        for index, curve in enumerate(curves):
            if day in curve:
                latest[index] = curve[day]
        equity = sum((e for e, _ in latest), ZERO)
        in_coins = sum((e * x for e, x in latest), ZERO)
        result.equity_curve.append((day, equity))
        result.exposure.append(in_coins / equity if equity > ZERO else ZERO)
    result.trades = [trade for part in parts for trade in part.trades]
    return result


@dataclass
class SplitResult:
    long: PortfolioResult
    short: PortfolioResult
    combined: PortfolioResult


def _subset(prepared: Mapping[str, Prepared], symbols: Sequence[str] | None) -> dict[str, Prepared]:
    return {s: p for s, p in prepared.items() if symbols is None or s in symbols}


def run_split(
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    plan: Accumulate,
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    long_pct: Decimal = Decimal("50"),
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    long_symbols: Sequence[str] | None = None,
) -> SplitResult:
    """``long_pct`` of ``capital`` bought and held as ``plan`` says, on
    ``long_symbols`` (default: every coin); the rest trading the breakout on
    every coin, sized off its own sleeve. Never rebalanced."""
    if not ZERO < long_pct < ONE_HUNDRED:
        raise ValueError("the long-term share must be a percent strictly between 0 and 100")
    long_capital = capital * long_pct / ONE_HUNDRED
    kwargs = dict(round_trip_pct=round_trip_pct, start=start, end=end, min_trade=min_trade)
    long = run_accumulate(_subset(prepared, long_symbols), plan, capital=long_capital, **kwargs)
    short = run_breakout(
        prepared, rule, capital=capital - long_capital, name="short-term: breakout", **kwargs
    )
    return SplitResult(long, short, combine(f"split: {plan.label} + breakout", [long, short]))


@dataclass
class BreakoutReport:
    base: PortfolioResult
    stressed: PortfolioResult
    hold: PortfolioResult
    hold_stressed: PortfolioResult
    #: The run with its most profitable coin left out, and which coin that was.
    without_best: tuple[str, PortfolioResult] | None = None


def evaluate(
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
) -> BreakoutReport:
    """The breakout and hold, normal and stressed, plus the breadth check."""
    stressed_trip = round_trip_pct * STRESS_FACTOR
    kwargs = dict(capital=capital, start=start, end=end)
    base = run_breakout(prepared, rule, round_trip_pct=round_trip_pct, min_trade=min_trade, **kwargs)
    report = BreakoutReport(
        base=base,
        stressed=run_breakout(
            prepared, rule, round_trip_pct=stressed_trip, min_trade=min_trade, **kwargs
        ),
        hold=run_equal_hold(prepared, round_trip_pct=round_trip_pct, **kwargs),
        hold_stressed=run_equal_hold(prepared, round_trip_pct=stressed_trip, **kwargs),
    )
    by_symbol = base.pnl_by_symbol()
    if len(prepared) > 1 and by_symbol:
        best = max(by_symbol, key=lambda s: by_symbol[s])
        rest = {s: p for s, p in prepared.items() if s != best}
        report.without_best = (
            best,
            run_breakout(rest, rule, round_trip_pct=round_trip_pct, min_trade=min_trade, **kwargs),
        )
    return report


def _r(value: Decimal | None) -> str:
    return "-" if value is None else f"{float(value):+.2f}R"


def _ratio(value: Decimal | None) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


_HEADER = (
    f"  {'strategy':<22}{'return':>9}{'max dd':>9}{'ret/dd':>8}{'trades':>8}{'open':>6}"
    f"{'win rate':>10}{'avg win':>9}{'avg loss':>10}{'expectancy':>12}{'exposure':>10}"
    f"{'stressed':>10}"
)


def _line(result: PortfolioResult, stressed: PortfolioResult) -> str:
    return (
        f"  {result.name:<22}{pct(result.total_return_pct):>9}"
        f"{pct(-result.max_drawdown_pct):>9}{_ratio(result.return_over_drawdown):>8}"
        f"{len(result.closed):>8}{sum(1 for t in result.trades if t.open):>6}"
        f"{_rate(result.win_rate):>10}{_r(result.average_win_r):>9}"
        f"{_r(result.average_loss_r):>10}{_r(result.expectancy_r):>12}"
        f"{float(result.average_exposure_pct):>9.0f}%{pct(stressed.total_return_pct):>10}"
    )


def render(report: BreakoutReport, rule: Breakout) -> str:
    base = report.base
    lines = [
        "=" * 110,
        "BREAKOUT PORTFOLIO",
        "=" * 110,
        f"One account of {money(base.capital)} trading {', '.join(base.symbols)} on UTC daily closes"
        + (f", {base.start:%Y-%m-%d} to {base.end:%Y-%m-%d}." if base.start and base.end else "."),
        rule.describe(),
        f"Every fill crosses half the {base.round_trip_pct}% round trip; 'stressed' re-runs at "
        f"{base.round_trip_pct * STRESS_FACTOR}%. Hold splits the same money evenly and keeps it.",
        "",
        "R is what a trade would lose at its initial stop. 'expectancy' is the mean R of closed",
        "trades after costs: above zero is an edge, whatever the position size. 'trades' counts",
        "closed ones; 'open' are still held at the end, marked at the bid.",
        "",
        _HEADER,
        _line(base, report.stressed),
        _line(report.hold, report.hold_stressed),
        "",
        f"  {'by coin':<22}{'trades':>8}{'net P&L':>11}{'win rate':>10}{'expectancy':>12}",
    ]
    for symbol in base.symbols:
        trades = [t for t in base.trades if t.symbol == symbol]
        closed = [t for t in trades if not t.open]
        wins = sum(1 for t in closed if t.pnl > ZERO)
        rs = [t.r for t in closed if t.r is not None]
        lines.append(
            f"  {symbol:<22}{len(closed):>8}{money(sum((t.pnl for t in trades), ZERO)):>11}"
            f"{_rate(wins / len(closed) if closed else None):>10}"
            f"{_r(sum(rs, ZERO) / Decimal(len(rs)) if rs else None):>12}"
        )
    if report.without_best is not None:
        best, rest = report.without_best
        lines += [
            "",
            f"  Without its most profitable coin ({best}): {pct(rest.total_return_pct)} return, "
            f"{_r(rest.expectancy_r)} expectancy on {len(rest.closed)} closed trades.",
        ]
    lines += [
        "",
        "A backtest is evidence about a rule on past prices, not about the next trade:",
        "it authorizes nothing. The bar this rule has to clear is in docs/strategy.md.",
    ]
    return "\n".join(lines)


@dataclass
class RollingBreakout:
    starts: list[datetime]
    returns: list[float]
    hold_returns: list[float]
    stressed_returns: list[float]
    drawdowns: list[float]
    hold_drawdowns: list[float]


def run_rolling(
    series: Mapping[str, Sequence[Candle]],
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    *,
    window: timedelta,
    step: timedelta,
    capital: Decimal = DEFAULT_CAPITAL,
    round_trip_pct: Decimal = Decimal("1.9"),
    min_trade: Decimal = DEFAULT_MIN_TRADE,
) -> RollingBreakout:
    """The breakout and hold over every window, each starting in cash."""
    out = RollingBreakout([], [], [], [], [], [])
    for start in window_starts(series, window=window, step=step):
        end = start + window
        kwargs = dict(capital=capital, start=start, end=end)
        base = run_breakout(
            prepared, rule, round_trip_pct=round_trip_pct, min_trade=min_trade, **kwargs
        )
        if not base.equity_curve:
            continue
        stressed = run_breakout(
            prepared, rule, round_trip_pct=round_trip_pct * STRESS_FACTOR, min_trade=min_trade,
            **kwargs,
        )
        hold = run_equal_hold(prepared, round_trip_pct=round_trip_pct, **kwargs)
        out.starts.append(start)
        out.returns.append(float(base.total_return_pct))
        out.stressed_returns.append(float(stressed.total_return_pct))
        out.hold_returns.append(float(hold.total_return_pct))
        out.drawdowns.append(float(base.max_drawdown_pct))
        out.hold_drawdowns.append(float(hold.max_drawdown_pct))
    return out


def _rolling_row(
    name: str, values: Sequence[float], drawdowns: Sequence[float], beat: str, *, width: int = 22
) -> str:
    count = len(values)
    positive = sum(1 for v in values if v > 0)
    return (
        f"  {name:<{width}}{count:>8}{f'{positive}/{count}':>10}{beat:>11}"
        f"{pct(statistics.median(values)):>9}{pct(min(values)):>9}{pct(max(values)):>9}"
        f"{pct(-statistics.median(drawdowns)):>12}{pct(-max(drawdowns)):>11}"
    )


def _rolling_header(width: int = 22) -> str:
    return (
        f"  {'strategy':<{width}}{'windows':>8}{'positive':>10}{'beat hold':>11}{'median':>9}"
        f"{'worst':>9}{'best':>9}{'median dd':>12}{'worst dd':>11}"
    )


def _beat(values: Sequence[float], hold: Sequence[float]) -> str:
    return f"{sum(1 for v, h in zip(values, hold, strict=True) if v > h)}/{len(values)}"


def render_rolling(rolling: RollingBreakout, *, window: timedelta, step: timedelta) -> str:
    count = len(rolling.returns)
    if count == 0:
        return (
            f"BREAKOUT ROLLING WINDOWS: no whole {window.days}-day window fits in the history."
        )
    return "\n".join(
        [
            "=" * 110,
            "BREAKOUT ROLLING WINDOWS",
            "=" * 110,
            f"{window.days}-day windows, a new one every {step.days} days: {count} "
            f"window{'' if count == 1 else 's'}. Each starts in cash, with its averages warm.",
            f"Stressed at {STRESS_FACTOR}x the round trip, the breakout beat hold in "
            f"{_beat(rolling.stressed_returns, rolling.hold_returns)}.",
            "",
            _rolling_header(),
            _rolling_row(
                "breakout", rolling.returns, rolling.drawdowns,
                _beat(rolling.returns, rolling.hold_returns),
            ),
            _rolling_row("hold (equal weight)", rolling.hold_returns, rolling.hold_drawdowns, "-"),
        ]
    )


# -- the split ---------------------------------------------------------------


@dataclass
class SplitReport:
    plan: Accumulate
    long_symbols: list[str]
    split: SplitResult
    split_stressed: SplitResult
    #: The long-term sleeve bought each way, (normal, stressed), by mode.
    long_variants: dict[str, tuple[PortfolioResult, PortfolioResult]]
    #: The whole account in the breakout, and in hold, (normal, stressed).
    all_breakout: tuple[PortfolioResult, PortfolioResult]
    all_hold: tuple[PortfolioResult, PortfolioResult]


def evaluate_split(
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    plan: Accumulate,
    *,
    capital: Decimal = DEFAULT_CAPITAL,
    long_pct: Decimal = Decimal("50"),
    round_trip_pct: Decimal = Decimal("1.9"),
    start: datetime | None = None,
    end: datetime | None = None,
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    long_symbols: Sequence[str] | None = None,
) -> SplitReport:
    """The split, and what it is weighed against: the long-term sleeve bought
    the other two ways, and the whole account in the breakout or in hold."""
    trips = (round_trip_pct, round_trip_pct * STRESS_FACTOR)
    window = dict(start=start, end=end)
    split, split_stressed = (
        run_split(
            prepared, rule, plan, capital=capital, long_pct=long_pct, round_trip_pct=trip,
            min_trade=min_trade, long_symbols=long_symbols, **window,
        )
        for trip in trips
    )
    long_prepared = _subset(prepared, long_symbols)
    variants: dict[str, tuple[PortfolioResult, PortfolioResult]] = {}
    for mode in MODES:
        if mode == plan.mode:
            variants[mode] = (split.long, split_stressed.long)
            continue
        other = replace(plan, mode=mode)
        base, stressed = (
            run_accumulate(
                long_prepared, other, capital=split.long.capital, round_trip_pct=trip,
                min_trade=min_trade, **window,
            )
            for trip in trips
        )
        variants[mode] = (base, stressed)
    breakout_runs = [
        run_breakout(
            prepared, rule, capital=capital, round_trip_pct=trip, min_trade=min_trade,
            name="all in the breakout", **window,
        )
        for trip in trips
    ]
    hold_runs = [
        run_equal_hold(prepared, capital=capital, round_trip_pct=trip, **window) for trip in trips
    ]
    for run in hold_runs:
        run.name = "all in hold (lump sum)"
    return SplitReport(
        plan=plan,
        long_symbols=sorted(long_prepared),
        split=split,
        split_stressed=split_stressed,
        long_variants=variants,
        all_breakout=(breakout_runs[0], breakout_runs[1]),
        all_hold=(hold_runs[0], hold_runs[1]),
    )


_SPLIT_WIDTH = 30


def _split_line(result: PortfolioResult, stressed: PortfolioResult) -> str:
    return (
        f"  {result.name:<{_SPLIT_WIDTH}}{money(result.capital):>10}{pct(result.total_return_pct):>9}"
        f"{pct(-result.max_drawdown_pct):>9}{_ratio(result.return_over_drawdown):>8}"
        f"{float(result.average_exposure_pct):>9.0f}%{float(result.final_exposure_pct):>8.0f}%"
        f"{money(result.final_equity):>11}{pct(stressed.total_return_pct):>10}"
    )


def render_split(report: SplitReport, rule: Breakout) -> str:
    split = report.split
    combined, long, short = split.combined, split.long, split.short
    span = (
        f", {combined.start:%Y-%m-%d} to {combined.end:%Y-%m-%d}"
        if combined.start and combined.end
        else ""
    )
    trip = combined.round_trip_pct
    lines = [
        "=" * 110,
        "SPLIT ACCOUNT: LONG-TERM + SHORT-TERM",
        "=" * 110,
        f"One {money(combined.capital)} account{span}, in two sleeves that never pass cash "
        "between them:",
        f"  long-term  {money(long.capital)} in {', '.join(report.long_symbols)}. "
        f"{report.plan.describe()}",
        f"  short-term {money(short.capital)} trading the breakout on {', '.join(short.symbols)}, "
        "sized off its own sleeve:",
        f"             {rule.describe()}",
        f"Every fill crosses half the {trip}% round trip; 'stressed' re-runs at "
        f"{trip * STRESS_FACTOR}%. 'in coins' is the average share held in coins,",
        "'at end' the share on the last day: the rest is cash, which for a long-term row is "
        "money it never spent.",
        "",
        f"  {'account':<{_SPLIT_WIDTH}}{'capital':>10}{'return':>9}{'max dd':>9}{'ret/dd':>8}"
        f"{'in coins':>10}{'at end':>9}{'final':>11}{'stressed':>10}",
    ]
    for mode in MODES:
        base, stressed = report.long_variants[mode]
        lines.append(_split_line(base, stressed))
    lines += [
        _split_line(short, report.split_stressed.short),
        _split_line(combined, report.split_stressed.combined),
        _split_line(*report.all_breakout),
        _split_line(*report.all_hold),
        "",
        f"The split buys its long-term sleeve the '{report.plan.label}' way; the other two "
        "long-term rows show the same sleeve bought differently.",
        "A backtest authorizes nothing. Running a split for real is the owner's decision, "
        "inside config/risk_limits.yaml",
        "(docs/strategy.md, 'The split').",
    ]
    return "\n".join(lines)


@dataclass
class RollingSplit:
    starts: list[datetime]
    #: Each row's (return, max drawdown) per window, in percent.
    rows: dict[str, list[tuple[float, float]]]
    hold: list[tuple[float, float]]


def run_rolling_split(
    series: Mapping[str, Sequence[Candle]],
    prepared: Mapping[str, Prepared],
    rule: Breakout,
    plan: Accumulate,
    *,
    window: timedelta,
    step: timedelta,
    capital: Decimal = DEFAULT_CAPITAL,
    long_pct: Decimal = Decimal("50"),
    round_trip_pct: Decimal = Decimal("1.9"),
    min_trade: Decimal = DEFAULT_MIN_TRADE,
    long_symbols: Sequence[str] | None = None,
) -> RollingSplit:
    """The split, each sleeve, and hold over every window, each starting in cash."""
    out = RollingSplit([], {}, [])
    for start in window_starts(series, window=window, step=step):
        end = start + window
        split = run_split(
            prepared, rule, plan, capital=capital, long_pct=long_pct,
            round_trip_pct=round_trip_pct, start=start, end=end, min_trade=min_trade,
            long_symbols=long_symbols,
        )
        if not split.combined.equity_curve:
            continue
        hold = run_equal_hold(
            prepared, capital=capital, round_trip_pct=round_trip_pct, start=start, end=end
        )
        out.starts.append(start)
        for result in (split.combined, split.long, split.short):
            out.rows.setdefault(result.name, []).append(
                (float(result.total_return_pct), float(result.max_drawdown_pct))
            )
        out.hold.append((float(hold.total_return_pct), float(hold.max_drawdown_pct)))
    return out


def render_rolling_split(rolling: RollingSplit, *, window: timedelta, step: timedelta) -> str:
    count = len(rolling.starts)
    if count == 0:
        return f"SPLIT ROLLING WINDOWS: no whole {window.days}-day window fits in the history."
    hold_returns = [r for r, _ in rolling.hold]
    lines = [
        "=" * 110,
        "SPLIT ROLLING WINDOWS",
        "=" * 110,
        f"{window.days}-day windows, a new one every {step.days} days: {count} "
        f"window{'' if count == 1 else 's'}. Each starts in cash; each sleeve's return is on "
        "its own money.",
        "",
        _rolling_header(_SPLIT_WIDTH),
    ]
    for name, values in rolling.rows.items():
        returns = [r for r, _ in values]
        lines.append(
            _rolling_row(
                name, returns, [d for _, d in values], _beat(returns, hold_returns),
                width=_SPLIT_WIDTH,
            )
        )
    lines.append(
        _rolling_row(
            "hold (equal weight)", hold_returns, [d for _, d in rolling.hold], "-",
            width=_SPLIT_WIDTH,
        )
    )
    return "\n".join(lines)


def _num(value: Decimal | None, places: int = 2) -> str | None:
    return None if value is None else f"{value:.{places}f}"


def _numbers(result: PortfolioResult) -> dict[str, object]:
    return {
        "return_pct": _num(result.total_return_pct),
        "max_drawdown_pct": _num(result.max_drawdown_pct),
        "return_over_drawdown": _num(result.return_over_drawdown),
        "closed_trades": len(result.closed),
        "open_trades": sum(1 for t in result.trades if t.open),
        "win_rate": result.win_rate,
        "expectancy_r": _num(result.expectancy_r, 3),
        "average_exposure_pct": _num(result.average_exposure_pct),
        "final_equity": _num(result.final_equity),
    }


def summary(report: BreakoutReport) -> dict[str, object]:
    """The breakout's numbers, JSON-safe."""
    base = report.base
    return {
        "symbols": base.symbols,
        "start": base.start.isoformat() if base.start else None,
        "end": base.end.isoformat() if base.end else None,
        "capital": _num(base.capital),
        "round_trip_pct": str(base.round_trip_pct),
        "breakout": _numbers(base),
        "breakout_stressed": _numbers(report.stressed),
        "hold": _numbers(report.hold),
        "hold_stressed": _numbers(report.hold_stressed),
        "pnl_by_symbol": {s: _num(v) for s, v in base.pnl_by_symbol().items()},
        "without_best": (
            {"symbol": report.without_best[0], **_numbers(report.without_best[1])}
            if report.without_best
            else None
        ),
    }


def _sleeve_numbers(result: PortfolioResult) -> dict[str, object]:
    return {
        "capital": _num(result.capital),
        **_numbers(result),
        "final_exposure_pct": _num(result.final_exposure_pct),
    }


def split_summary(report: SplitReport) -> dict[str, object]:
    """The split's numbers, JSON-safe."""
    split = report.split
    return {
        "long_mode": report.plan.mode,
        "long_symbols": report.long_symbols,
        "start": split.combined.start.isoformat() if split.combined.start else None,
        "end": split.combined.end.isoformat() if split.combined.end else None,
        "split": _sleeve_numbers(split.combined),
        "split_stressed": _sleeve_numbers(report.split_stressed.combined),
        "long_term": {mode: _sleeve_numbers(base) for mode, (base, _) in report.long_variants.items()},
        "short_term": _sleeve_numbers(split.short),
        "all_breakout": _sleeve_numbers(report.all_breakout[0]),
        "all_hold": _sleeve_numbers(report.all_hold[0]),
    }
