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
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Sequence

from .backtest import STRESS_FACTOR, money, pct, window_starts
from .models import Candle
from .numeric import ZERO
from .strategy.breakout import Breakout, DayView, daily_bars, day_views

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
    half = round_trip_pct / Decimal(200)
    symbols = sorted(prepared)
    result = PortfolioResult("hold (equal weight)", symbols, capital, round_trip_pct)
    days, closes = _days(prepared, start, end)
    share = capital / Decimal(len(symbols)) if symbols else ZERO
    cash = capital
    held: dict[str, tuple[Decimal, datetime]] = {}
    last_close: dict[str, Decimal] = {}
    for day in days:
        for symbol in symbols:
            close = closes[symbol].get(day)
            if close is None:
                continue
            last_close[symbol] = close
            if symbol not in held:
                held[symbol] = (share / (close * (1 + half)), day)
                cash -= share
        worth = sum(
            (q * last_close[s] * (1 - half) for s, (q, _) in held.items()), ZERO
        )
        account = cash + worth
        result.equity_curve.append((day, account))
        result.exposure.append(worth / account if account > ZERO else ZERO)
    for symbol, (quantity, bought) in sorted(held.items()):
        proceeds = quantity * last_close[symbol] * (1 - half)
        result.trades.append(Trade(symbol, bought, None, quantity, share, proceeds, ZERO))
    return result


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


def render_rolling(rolling: RollingBreakout, *, window: timedelta, step: timedelta) -> str:
    count = len(rolling.returns)
    if count == 0:
        return (
            f"BREAKOUT ROLLING WINDOWS: no whole {window.days}-day window fits in the history."
        )

    def row(name: str, values: list[float], drawdowns: list[float], beat: str) -> str:
        positive = sum(1 for v in values if v > 0)
        return (
            f"  {name:<22}{count:>8}{f'{positive}/{count}':>10}{beat:>11}"
            f"{pct(statistics.median(values)):>9}{pct(min(values)):>9}{pct(max(values)):>9}"
            f"{pct(-statistics.median(drawdowns)):>12}{pct(-max(drawdowns)):>11}"
        )

    beat = sum(1 for r, h in zip(rolling.returns, rolling.hold_returns, strict=True) if r > h)
    beat_stressed = sum(
        1 for r, h in zip(rolling.stressed_returns, rolling.hold_returns, strict=True) if r > h
    )
    return "\n".join(
        [
            "=" * 110,
            "BREAKOUT ROLLING WINDOWS",
            "=" * 110,
            f"{window.days}-day windows, a new one every {step.days} days: {count} "
            f"window{'' if count == 1 else 's'}. Each starts in cash, with its averages warm.",
            f"Stressed at {STRESS_FACTOR}x the round trip, the breakout beat hold in "
            f"{beat_stressed}/{count}.",
            "",
            f"  {'strategy':<22}{'windows':>8}{'positive':>10}{'beat hold':>11}{'median':>9}"
            f"{'worst':>9}{'best':>9}{'median dd':>12}{'worst dd':>11}",
            row("breakout", rolling.returns, rolling.drawdowns, f"{beat}/{count}"),
            row("hold (equal weight)", rolling.hold_returns, rolling.hold_drawdowns, "-"),
        ]
    )


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
