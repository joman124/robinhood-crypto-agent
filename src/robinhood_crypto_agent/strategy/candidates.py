"""Candidate rules faster than the live split. Backtest-only: nothing live imports this.

``docs/candidates.md`` fixes what each candidate is and the bar it is judged
against, before any of them was run on real bars. ``candidate_backtest.py``
runs them. Every rule here decides on closed bars, is long only, and sizes
like the breakout: a trade risks a fixed share of the account at its initial
stop, capped per coin.

A rule gives the backtest four things: its bars and a view of each bar's
indicators (:meth:`prepare`), whether a view enters, how far under the close
its initial stop sits (which sizes the trade and defines one R), and whether
a held position exits on a view. "The daily trend is up" is always the
*previous* UTC day's close against its 100-day average, so no decision reads
a day that is still forming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, Sequence

from ..models import Candle
from ..numeric import ZERO
from .breakout import Breakout, daily_bars, day_views
from .hodl import moving_averages

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
ONE_HUNDRED = Decimal("100")
#: The daily trend filter's average, in days: the breakout's regime average.
TREND_DAYS = 100
#: Daily bars a rule with the daily trend filter needs before its first decision.
TREND_WARMUP_DAYS = TREND_DAYS + 2


@dataclass
class Held:
    """A position the backtest holds, as a rule's exit sees it."""

    symbol: str
    quantity: Decimal
    #: The start of the bar it filled on.
    entered: datetime
    entry_price: Decimal
    #: Dollars paid, costs included.
    cost: Decimal
    #: Dollars it would lose at its initial stop: one R.
    risk: Decimal
    #: The highest close since it filled, its fill price included.
    highest: Decimal
    #: The view that decided the entry.
    entry_view: Any
    #: Bars closed since the one it filled on.
    bars: int = 0


class Rule(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def interval_minutes(self) -> int: ...

    @property
    def warmup_days(self) -> int:
        """Days of history needed before the first decision."""
        ...

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        """The rule's bars, and a view of each decidable one keyed by its start."""
        ...

    def enters(self, view: Any) -> bool: ...

    def stop_fraction(self, view: Any) -> Decimal:
        """How far under the close the initial stop sits, as a fraction."""
        ...

    def position_dollars(self, equity: Decimal, view: Any) -> Decimal: ...

    def exits(self, held: Held, view: Any) -> bool: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class Sizing:
    """The breakout's sizing: ``risk_pct`` of equity lost at the initial stop,
    at most ``max_weight_pct`` of equity in one coin."""

    risk_pct: Decimal = Decimal("1")
    max_weight_pct: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if not ZERO < self.risk_pct <= ONE_HUNDRED:
            raise ValueError("risk per trade must be a percent in (0, 100]")
        if not ZERO < self.max_weight_pct <= ONE_HUNDRED:
            raise ValueError("the per-coin cap must be a percent in (0, 100]")

    def dollars(self, equity: Decimal, stop_fraction: Decimal) -> Decimal:
        if equity <= ZERO or stop_fraction <= ZERO:
            return ZERO
        by_risk = equity * self.risk_pct / ONE_HUNDRED / stop_fraction
        return min(by_risk, equity * self.max_weight_pct / ONE_HUNDRED)


# -- bars and indicators -----------------------------------------------------


def resample(hourly: Sequence[Candle], minutes: int) -> list[Candle]:
    """Bars of ``minutes`` from hourly ones, aligned to UTC midnight: first
    open, highest high, lowest low, last close. A bar that has not closed yet
    is left out, as ``daily_bars`` leaves out a day still forming."""
    if minutes % 60 or (24 * 60) % minutes:
        raise ValueError("a bar must be a whole number of hours that divides a day")
    if not hourly:
        return []
    span = timedelta(minutes=minutes)
    buckets: dict[datetime, list[Candle]] = {}
    for candle in hourly:
        start = candle.start.astimezone(timezone.utc)
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket = midnight + span * ((start - midnight) // span)
        buckets.setdefault(bucket, []).append(candle)
    last_end = max(c.end for c in hourly)
    bars: list[Candle] = []
    for bucket in sorted(buckets):
        if last_end < bucket + span:
            continue
        group = sorted(buckets[bucket], key=lambda c: c.start)
        bars.append(
            Candle(
                symbol=group[0].symbol,
                start=bucket,
                end=bucket + span,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                observations=len(group),
            )
        )
    return bars


def true_ranges(bars: Sequence[Candle]) -> list[Decimal]:
    ranges: list[Decimal] = []
    for index, bar in enumerate(bars):
        if index == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous = bars[index - 1].close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return ranges


def rolling_mean(values: Sequence[Decimal], count: int) -> list[Decimal | None]:
    """The mean of the last ``count`` values at each index, itself included;
    ``None`` until there are that many."""
    means: list[Decimal | None] = []
    total = ZERO
    for index, value in enumerate(values):
        total += value
        if index >= count:
            total -= values[index - count]
        means.append(total / Decimal(count) if index >= count - 1 else None)
    return means


def utc_day(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def daily_trend(hourly: Sequence[Candle]) -> dict[datetime, bool]:
    """For each UTC day, whether the day *before* it closed above its 100-day
    average. A day missing here has no trend to read, which reads as "not up"."""
    daily = daily_bars(hourly)
    averages = moving_averages(daily, TREND_DAYS)
    return {
        bar.start + DAY: bar.close > averages[bar.start] for bar in daily if bar.start in averages
    }


# -- the rules ---------------------------------------------------------------


@dataclass(frozen=True)
class DailyBreakout:
    """The live breakout (``strategy/breakout.py``), unchanged, on daily closes."""

    rule: Breakout = field(default_factory=Breakout)

    @property
    def name(self) -> str:
        return f"breakout {self.rule.breakout_days}d"

    @property
    def interval_minutes(self) -> int:
        return 24 * 60

    @property
    def warmup_days(self) -> int:
        return self.rule.warmup_days + 2

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        daily = daily_bars(hourly)
        return daily, dict(day_views(daily, self.rule))

    def enters(self, view: Any) -> bool:
        return self.rule.enters(view)

    def stop_fraction(self, view: Any) -> Decimal:
        return self.rule.stop_fraction(view)

    def position_dollars(self, equity: Decimal, view: Any) -> Decimal:
        return self.rule.position_dollars(equity, view)

    def exits(self, held: Held, view: Any) -> bool:
        return self.rule.exits(held.highest, view)

    def describe(self) -> str:
        return self.rule.describe()


@dataclass(frozen=True)
class BarView:
    start: datetime
    close: Decimal
    #: The highest close of the previous ``breakout_bars`` bars, this one excluded.
    prior_high: Decimal
    #: The mean true range of the last ``atr_bars`` bars, this one included.
    atr: Decimal
    trend_up: bool


@dataclass(frozen=True)
class FourHourBreakout:
    """The breakout on 4-hour closes, in a daily uptrend, with a time stop."""

    breakout_bars: int = 20
    atr_bars: int = 20
    stop_atr: Decimal = Decimal("3")
    #: Exit at the close of this many bars after the fill, whatever the price.
    max_bars: int = 30
    sizing: Sizing = field(default_factory=Sizing)

    def __post_init__(self) -> None:
        if min(self.breakout_bars, self.atr_bars, self.max_bars) < 1:
            raise ValueError("the breakout, ATR and holding windows must be at least one bar")
        if self.stop_atr <= ZERO:
            raise ValueError("the stop must be a positive number of ATRs")

    @property
    def name(self) -> str:
        return f"4h breakout {self.breakout_bars} bars"

    @property
    def interval_minutes(self) -> int:
        return 240

    @property
    def warmup_days(self) -> int:
        return TREND_WARMUP_DAYS

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        bars = resample(hourly, self.interval_minutes)
        trend = daily_trend(hourly)
        atrs = rolling_mean(true_ranges(bars), self.atr_bars)
        closes = [bar.close for bar in bars]
        views: dict[datetime, Any] = {}
        for index in range(max(self.breakout_bars, self.atr_bars - 1), len(bars)):
            bar, atr = bars[index], atrs[index]
            assert atr is not None
            views[bar.start] = BarView(
                start=bar.start,
                close=bar.close,
                prior_high=max(closes[index - self.breakout_bars : index]),
                atr=atr,
                trend_up=trend.get(utc_day(bar.start), False),
            )
        return bars, views

    def enters(self, view: BarView) -> bool:
        return view.atr > ZERO and view.trend_up and view.close > view.prior_high

    def stop_fraction(self, view: BarView) -> Decimal:
        return self.stop_atr * view.atr / view.close

    def position_dollars(self, equity: Decimal, view: BarView) -> Decimal:
        return self.sizing.dollars(equity, self.stop_fraction(view))

    def exits(self, held: Held, view: BarView) -> bool:
        return held.bars >= self.max_bars or view.close < held.highest - self.stop_atr * view.atr

    def describe(self) -> str:
        return (
            f"Buy a 4h close above the prior {self.breakout_bars} bars' closes while the daily "
            f"trend is up. Sell on a close {self.stop_atr} x ATR({self.atr_bars} bars) under the "
            f"highest close since entry, or after {self.max_bars} bars."
        )


@dataclass(frozen=True)
class DayBarView:
    start: datetime
    close: Decimal
    day: datetime
    day_open: Decimal
    #: The day's open plus ``k`` x the previous day's range; ``None`` without one.
    level: Decimal | None
    trend_up: bool
    #: The day's first hourly close above ``level``.
    first_cross: bool
    #: The 23:00 UTC bar, whose close is the day's.
    last_of_day: bool


@dataclass(frozen=True)
class DayBreakout:
    """The UTC-day volatility breakout: in on the first hourly close a set
    share of yesterday's range above today's open, out by the day's close."""

    k: Decimal = Decimal("0.5")
    sizing: Sizing = field(default_factory=Sizing)

    def __post_init__(self) -> None:
        if self.k <= ZERO:
            raise ValueError("k must be a positive share of the previous day's range")

    @property
    def name(self) -> str:
        return f"day breakout k={self.k}"

    @property
    def interval_minutes(self) -> int:
        return 60

    @property
    def warmup_days(self) -> int:
        return TREND_WARMUP_DAYS

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        bars = resample(hourly, self.interval_minutes)
        trend = daily_trend(hourly)
        previous_range = {bar.start + DAY: bar.high - bar.low for bar in daily_bars(hourly)}
        views: dict[datetime, Any] = {}
        day: datetime | None = None
        day_open = ZERO
        crossed = False
        for bar in bars:
            today = utc_day(bar.start)
            if today != day:
                day, day_open, crossed = today, bar.open, False
            span = previous_range.get(today)
            level = None if span is None else day_open + self.k * span
            above = level is not None and bar.close > level
            views[bar.start] = DayBarView(
                start=bar.start,
                close=bar.close,
                day=today,
                day_open=day_open,
                level=level,
                trend_up=trend.get(today, False),
                first_cross=above and not crossed,
                last_of_day=bar.start.astimezone(timezone.utc).hour == 23,
            )
            crossed = crossed or above
        return bars, views

    def enters(self, view: DayBarView) -> bool:
        return view.first_cross and view.trend_up and not view.last_of_day

    def stop_fraction(self, view: DayBarView) -> Decimal:
        return (view.close - view.day_open) / view.close

    def position_dollars(self, equity: Decimal, view: DayBarView) -> Decimal:
        return self.sizing.dollars(equity, self.stop_fraction(view))

    def exits(self, held: Held, view: DayBarView) -> bool:
        entry = held.entry_view
        return view.day != entry.day or view.close < entry.day_open or view.last_of_day

    def describe(self) -> str:
        return (
            f"Each UTC day, buy the first hourly close above the day's open plus {self.k} x the "
            "previous day's range, while the daily trend is up. Sell on an hourly close under "
            "the day's open, or at the day's last close."
        )


@dataclass(frozen=True)
class DipView:
    start: datetime
    close: Decimal
    #: The mean and the population standard deviation of the last ``bars`` closes.
    mean: Decimal
    deviation: Decimal
    atr: Decimal
    trend_up: bool


@dataclass(frozen=True)
class DipInUptrend:
    """Mean reversion on hourly closes, only while the daily trend is up."""

    z: Decimal = Decimal("2")
    bars: int = 20
    stop_atr: Decimal = Decimal("3")
    max_bars: int = 24
    sizing: Sizing = field(default_factory=Sizing)

    def __post_init__(self) -> None:
        if self.z <= ZERO or self.stop_atr <= ZERO:
            raise ValueError("the dip and the stop must be positive")
        if self.bars < 2 or self.max_bars < 1:
            raise ValueError("the mean needs two bars, and the hold at least one")

    @property
    def name(self) -> str:
        return f"dip in uptrend z={self.z}"

    @property
    def interval_minutes(self) -> int:
        return 60

    @property
    def warmup_days(self) -> int:
        return TREND_WARMUP_DAYS

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        bars = resample(hourly, self.interval_minutes)
        trend = daily_trend(hourly)
        atrs = rolling_mean(true_ranges(bars), self.bars)
        closes = [bar.close for bar in bars]
        count = Decimal(self.bars)
        views: dict[datetime, Any] = {}
        for index in range(self.bars - 1, len(bars)):
            window = closes[index - self.bars + 1 : index + 1]
            mean = sum(window, ZERO) / count
            variance = sum(((c - mean) ** 2 for c in window), ZERO) / count
            atr = atrs[index]
            assert atr is not None
            bar = bars[index]
            views[bar.start] = DipView(
                start=bar.start,
                close=bar.close,
                mean=mean,
                deviation=variance.sqrt(),
                atr=atr,
                trend_up=trend.get(utc_day(bar.start), False),
            )
        return bars, views

    def enters(self, view: DipView) -> bool:
        return (
            view.atr > ZERO
            and view.deviation > ZERO
            and view.trend_up
            and view.close < view.mean - self.z * view.deviation
        )

    def stop_fraction(self, view: DipView) -> Decimal:
        return self.stop_atr * view.atr / view.close

    def position_dollars(self, equity: Decimal, view: DipView) -> Decimal:
        return self.sizing.dollars(equity, self.stop_fraction(view))

    def exits(self, held: Held, view: DipView) -> bool:
        entry = held.entry_view
        return (
            held.bars >= self.max_bars
            or view.close >= view.mean
            or view.close < entry.close - self.stop_atr * entry.atr
        )

    def describe(self) -> str:
        return (
            f"Buy an hourly close {self.z} standard deviations under its {self.bars}-bar mean "
            f"while the daily trend is up. Sell when a close is back at the mean, on a close "
            f"{self.stop_atr} x ATR({self.bars} bars) under the entry, or after "
            f"{self.max_bars} bars."
        )


@dataclass(frozen=True)
class Hours:
    """Another rule, entering only on bars that start from ``first_hour`` to
    ``last_hour`` UTC, both included. Exits are the rule's own, at any hour."""

    rule: Any
    first_hour: int = 13
    last_hour: int = 16

    def __post_init__(self) -> None:
        if not 0 <= self.first_hour <= self.last_hour <= 23:
            raise ValueError("the hours must run forward within one UTC day")

    @property
    def name(self) -> str:
        return f"{self.rule.name}, {self.first_hour:02d}-{self.last_hour + 1:02d} UTC"

    @property
    def interval_minutes(self) -> int:
        return int(self.rule.interval_minutes)

    @property
    def warmup_days(self) -> int:
        return int(self.rule.warmup_days)

    def prepare(self, hourly: Sequence[Candle]) -> tuple[list[Candle], dict[datetime, Any]]:
        bars, views = self.rule.prepare(hourly)
        return bars, views

    def enters(self, view: Any) -> bool:
        hour = view.start.astimezone(timezone.utc).hour
        return self.first_hour <= hour <= self.last_hour and bool(self.rule.enters(view))

    def stop_fraction(self, view: Any) -> Decimal:
        return Decimal(self.rule.stop_fraction(view))

    def position_dollars(self, equity: Decimal, view: Any) -> Decimal:
        return Decimal(self.rule.position_dollars(equity, view))

    def exits(self, held: Held, view: Any) -> bool:
        return bool(self.rule.exits(held, view))

    def describe(self) -> str:
        return (
            f"{self.rule.describe()} Enter only on bars starting {self.first_hour:02d}:00-"
            f"{self.last_hour:02d}:00 UTC."
        )


# -- the candidates, as docs/candidates.md fixes them ------------------------

#: Candidate 2's coins: the watchlist and five more deep pairs.
BREADTH_SYMBOLS = (
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "LTC-USD", "AVAX-USD", "LINK-USD",
    "BCH-USD",
)
#: Closed trades the edge check asks of a daily candidate, and of a faster one.
DAILY_TRADES = 30
FAST_TRADES = 100


@dataclass(frozen=True)
class Candidate:
    key: str
    name: str
    #: One rule, or several sleeves that each trade an equal share of the account.
    sleeves: tuple[Any, ...]
    #: Closed trades the edge check asks for.
    min_trades: int
    #: Enter only when the stop is at least this many round trips away.
    min_stop_trips: Decimal | None = None
    #: The coins it trades, when not the run's own.
    symbols: tuple[str, ...] | None = None
    #: The same candidate either side of its key parameter, for the plateau check.
    neighbours: tuple["Candidate", ...] = ()

    @property
    def rules(self) -> list[Any]:
        """Every rule it and its neighbours run."""
        rules = list(self.sleeves)
        for neighbour in self.neighbours:
            rules.extend(r for r in neighbour.sleeves if r not in rules)
        return rules

    @property
    def warmup_days(self) -> int:
        return max(rule.warmup_days for rule in self.rules)


def _breakout(days: int = 20) -> DailyBreakout:
    return DailyBreakout(Breakout(breakout_days=days))


def _alone(key: str, name: str, rule: Any, **kwargs: Any) -> Candidate:
    """A neighbour: the candidate's settings with one parameter moved."""
    trades = DAILY_TRADES if rule.interval_minutes == 24 * 60 else FAST_TRADES
    return Candidate(key, name, (rule,), trades, **kwargs)


def _candidates() -> tuple[Candidate, ...]:
    live = _breakout()
    four_hour = FourHourBreakout()
    day = DayBreakout()
    dip = DipInUptrend()
    return (
        Candidate(
            "ref", "breakout (live)", (live,), DAILY_TRADES,
            neighbours=(
                _alone("ref", "10-day high", _breakout(10)),
                _alone("ref", "30-day high", _breakout(30)),
            ),
        ),
        Candidate(
            "1", "cost filter", (live,), DAILY_TRADES, min_stop_trips=Decimal("4"),
            neighbours=(
                _alone("1", "2 round trips", live, min_stop_trips=Decimal("2")),
                _alone("1", "6 round trips", live, min_stop_trips=Decimal("6")),
            ),
        ),
        Candidate(
            "2", "breadth", (live,), DAILY_TRADES, symbols=BREADTH_SYMBOLS,
            neighbours=(
                _alone("2", "10-day high", _breakout(10), symbols=BREADTH_SYMBOLS),
                _alone("2", "30-day high", _breakout(30), symbols=BREADTH_SYMBOLS),
            ),
        ),
        Candidate("3", "blend 10/20/55", (_breakout(10), live, _breakout(55)), DAILY_TRADES),
        Candidate(
            "4", "4h breakout", (four_hour,), FAST_TRADES,
            neighbours=(
                _alone("4", "10 bars", FourHourBreakout(breakout_bars=10)),
                _alone("4", "30 bars", FourHourBreakout(breakout_bars=30)),
            ),
        ),
        Candidate(
            "5", "day breakout", (day,), FAST_TRADES,
            neighbours=(
                _alone("5", "k=0.25", DayBreakout(k=Decimal("0.25"))),
                _alone("5", "k=0.75", DayBreakout(k=Decimal("0.75"))),
            ),
        ),
        Candidate(
            "6", "dip in uptrend", (dip,), FAST_TRADES,
            neighbours=(
                _alone("6", "z=1", DipInUptrend(z=Decimal("1"))),
                _alone("6", "z=3", DipInUptrend(z=Decimal("3"))),
            ),
        ),
        Candidate(
            "7a", "day breakout, 13-17 UTC", (Hours(day),), FAST_TRADES,
            neighbours=(
                _alone("7a", "k=0.25", Hours(DayBreakout(k=Decimal("0.25")))),
                _alone("7a", "k=0.75", Hours(DayBreakout(k=Decimal("0.75")))),
            ),
        ),
        Candidate(
            "7b", "dip in uptrend, 13-17 UTC", (Hours(dip),), FAST_TRADES,
            neighbours=(
                _alone("7b", "z=1", Hours(DipInUptrend(z=Decimal("1")))),
                _alone("7b", "z=3", Hours(DipInUptrend(z=Decimal("3")))),
            ),
        ),
    )


CANDIDATES = _candidates()
CANDIDATE_KEYS = tuple(c.key for c in CANDIDATES)


def select(keys: Sequence[str] | None) -> list[Candidate]:
    """The candidates named by ``keys``, in the document's order; all of them for ``None``."""
    if not keys:
        return list(CANDIDATES)
    unknown = sorted(set(keys) - set(CANDIDATE_KEYS))
    if unknown:
        raise ValueError(f"unknown candidates {unknown}; choose from {list(CANDIDATE_KEYS)}")
    return [c for c in CANDIDATES if c.key in keys]
