"""The trend breakout: buy strength, cut losers with a trailing stop, size by risk.

A candidate successor to the trend ladder, built point by point from the
ladder's critique (docs/strategy.md, "The breakout candidate"). It is
backtest-only: nothing live trades it.

Once a day, on each coin's UTC daily close:

- **Enter** a coin that closes above every close of the previous 20 days (a
  20-day closing high) *and* above its 100-day average. It buys strength in
  an established uptrend, where the ladder bought dips that were often the
  start of a decline.
- **Size** it so that a stop 3 x ATR(20) under the entry would lose a fixed
  share of the account (1%), and never more than the per-coin cap. A calm coin
  gets a bigger position than a wild one, and every trade risks the same.
- **Exit** when the close falls 3 x ATR(20) under the highest close since
  entry: a chandelier stop. It trails the winners up and cuts the losers,
  with no profit target and no partial sells.

Entry and exit use different conditions -- a new 20-day high in, a fall of
three average daily ranges out -- so a price hovering near one line cannot
buy and sell on alternate days, as the ladder's trend exit did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from ..models import Candle
from ..numeric import ZERO

DAY = timedelta(days=1)
ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class DayView:
    """One coin's indicators as of one daily close."""

    day: datetime
    close: Decimal
    #: The highest close of the previous ``breakout_days`` days, today excluded.
    prior_high: Decimal
    #: The mean close of the last ``regime_days`` days, today included.
    average: Decimal
    #: The mean true range of the last ``atr_days`` days, today included.
    atr: Decimal


@dataclass(frozen=True)
class Breakout:
    """The rule's settings. Fixed before any backtest result was seen."""

    breakout_days: int = 20
    regime_days: int = 100
    atr_days: int = 20
    stop_atr: Decimal = Decimal("3")
    #: Percent of the account a trade loses if it exits at its initial stop.
    risk_pct: Decimal = Decimal("1")
    #: The most of the account one coin may hold, in percent.
    max_weight_pct: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if min(self.breakout_days, self.regime_days, self.atr_days) < 1:
            raise ValueError("the breakout, regime and ATR windows must be at least one day")
        if self.stop_atr <= ZERO:
            raise ValueError("the stop must be a positive number of ATRs")
        if not ZERO < self.risk_pct <= ONE_HUNDRED:
            raise ValueError("risk per trade must be a percent in (0, 100]")
        if not ZERO < self.max_weight_pct <= ONE_HUNDRED:
            raise ValueError("the per-coin cap must be a percent in (0, 100]")

    @property
    def warmup_days(self) -> int:
        """Daily bars needed before the first decision."""
        return max(self.breakout_days, self.regime_days - 1, self.atr_days)

    def enters(self, view: DayView) -> bool:
        """A 20-day closing high, above the 100-day average."""
        return (
            view.atr > ZERO
            and view.close > view.prior_high
            and view.close > view.average
        )

    def stop(self, highest: Decimal, view: DayView) -> Decimal:
        """The chandelier stop: ``stop_atr`` ATRs under the highest close."""
        return highest - self.stop_atr * view.atr

    def exits(self, highest: Decimal, view: DayView) -> bool:
        return view.close < self.stop(highest, view)

    def stop_fraction(self, view: DayView) -> Decimal:
        """How far under the close the initial stop sits, as a fraction."""
        return self.stop_atr * view.atr / view.close

    def position_dollars(self, equity: Decimal, view: DayView) -> Decimal:
        """Dollars to put in: ``risk_pct`` of equity lost at the initial stop,
        capped at ``max_weight_pct`` of equity."""
        if equity <= ZERO or view.atr <= ZERO:
            return ZERO
        by_risk = equity * self.risk_pct / ONE_HUNDRED / self.stop_fraction(view)
        return min(by_risk, equity * self.max_weight_pct / ONE_HUNDRED)

    def describe(self) -> str:
        return (
            f"Buy a coin on a daily close above its prior {self.breakout_days}-day high and its "
            f"{self.regime_days}-day average. Size it so a stop {self.stop_atr} x ATR({self.atr_days}) "
            f"under the entry risks {self.risk_pct}% of the account, at most "
            f"{self.max_weight_pct}% of it in one coin. Sell on a close "
            f"{self.stop_atr} x ATR({self.atr_days}) under the highest close since entry."
        )


def daily_bars(hourly: Sequence[Candle]) -> list[Candle]:
    """UTC daily bars from hourly ones: first open, highest high, lowest low,
    last close. A day that has not closed yet -- the last bar ends before
    midnight -- is left out, so no decision reads a close that is still moving."""
    if not hourly:
        return []
    by_day: dict[datetime, list[Candle]] = {}
    for candle in hourly:
        start = candle.start.astimezone(timezone.utc)
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, []).append(candle)
    last_end = max(c.end for c in hourly)
    days: list[Candle] = []
    for day in sorted(by_day):
        if last_end < day + DAY:
            continue
        bars = sorted(by_day[day], key=lambda c: c.start)
        days.append(
            Candle(
                symbol=bars[0].symbol,
                start=day,
                end=day + DAY,
                open=bars[0].open,
                high=max(c.high for c in bars),
                low=min(c.low for c in bars),
                close=bars[-1].close,
                observations=len(bars),
            )
        )
    return days


def day_views(daily: Sequence[Candle], rule: Breakout) -> dict[datetime, DayView]:
    """Each daily close's indicators, keyed by the day's start. Days without
    enough history behind them have no entry, which reads as "no decision"."""
    closes = [c.close for c in daily]
    ranges: list[Decimal] = []
    for index, bar in enumerate(daily):
        if index == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous = closes[index - 1]
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))

    views: dict[datetime, DayView] = {}
    for index in range(rule.warmup_days, len(daily)):
        window = closes[index - rule.regime_days + 1 : index + 1]
        views[daily[index].start] = DayView(
            day=daily[index].start,
            close=closes[index],
            prior_high=max(closes[index - rule.breakout_days : index]),
            average=sum(window, ZERO) / Decimal(rule.regime_days),
            atr=sum(ranges[index - rule.atr_days + 1 : index + 1], ZERO) / Decimal(rule.atr_days),
        )
    return views
