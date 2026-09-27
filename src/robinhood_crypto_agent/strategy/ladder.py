"""The trend ladder: buy dips in dollar steps while the trend is up, sell into strength.

This is the rule the agent trades, and the rule ``rhca backtest`` replays. Both
call :meth:`Ladder.decide` with the same inputs, so a backtest is evidence about
exactly the proposals the agent makes, not about a lookalike.

The rule, on each closed bar
----------------------------
**The anchor.** While nothing is held, the anchor is the highest close since
the last position was sold (or since the ladder started). A dip is measured
from it. Once a step is bought the anchor is fixed until the position is empty
again: that span is one *cycle*.

**Buys.** Step ``i`` is ``(percent, dollars)``: buy ``dollars`` on a close
``percent`` or more under the anchor. Each step buys once per cycle. With the
trend filter, a buy needs a close *above* the average of the last
``trend_days`` of closes. Below it, or before the average exists, nothing is
bought.

**Sells**, in one of two modes:

* ``anchor`` -- measured from the same anchor as the buys: sell step ``i``'s
  dollars on a close ``percent`` over the anchor, and everything left at the
  last step. Each step sells once per cycle. This is the mode the agent trades.
* ``lot`` -- each lot sells, whole, once the close is its own step over the
  close it was bought at. The step then re-arms. Backtest only.

**The trend exit.** With it on, a close at or under the trend average sells
everything held. The filter alone only stops *buying* in a downtrend, so a
lot bought just before the turn used to ride the whole decline down. The exit
closes it instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from ..models import Candle, Side
from ..numeric import ZERO

#: (percent from the anchor, dollars) -- the steps the owner proposed.
DEFAULT_STEPS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("5"), Decimal("5")),
    (Decimal("10"), Decimal("10")),
    (Decimal("20"), Decimal("20")),
)

MODE_LOT = "lot"
MODE_ANCHOR = "anchor"
MODES = (MODE_LOT, MODE_ANCHOR)

#: Why an order was proposed. Also the ``rule`` written to the audit log.
REASON_DIP = "dip"
REASON_TAKE_PROFIT = "take_profit"
REASON_TREND_EXIT = "trend_exit"

ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class HeldLot:
    """A lot as the ``lot`` mode sees it: which step bought it, at what close."""

    step: int
    mark: Decimal
    key: object = None


@dataclass(frozen=True)
class Order:
    """One order the rule wants on this bar. Sizing and pricing happen later."""

    side: Side
    reason: str
    step: int | None = None
    #: A buy spends this; an ``anchor`` sell sells this much worth at the bid.
    dollars: Decimal | None = None
    #: Sell everything held.
    everything: bool = False
    #: ``lot`` mode: the lot to sell, whole.
    lot: object = None


@dataclass(frozen=True)
class Ladder:
    """The rule's settings. Stateless: what is held and bought is passed in."""

    steps: tuple[tuple[Decimal, Decimal], ...] = DEFAULT_STEPS
    mode: str = MODE_ANCHOR
    #: Buy only on a close above the trend average.
    trend_filter: bool = True
    #: Sell everything on a close at or under the trend average.
    trend_exit: bool = True

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"ladder mode must be one of {MODES}, got {self.mode!r}")
        if not self.steps:
            raise ValueError("the ladder needs at least one step")
        if any(pct <= ZERO or dollars <= ZERO for pct, dollars in self.steps):
            raise ValueError("every ladder step must be a positive percent and dollar amount")
        if tuple(sorted(self.steps)) != tuple(self.steps):
            raise ValueError("ladder steps must be in increasing order of percent")
        if self.trend_exit and not self.trend_filter:
            raise ValueError("the trend exit needs the trend filter: there is no average to exit on")

    @property
    def total_dollars(self) -> Decimal:
        """The most one cycle can put in."""
        return sum((dollars for _, dollars in self.steps), ZERO)

    def buy_level(self, anchor: Decimal, step: int) -> Decimal:
        """The close at or under which ``step`` buys."""
        return anchor * (Decimal(1) - self.steps[step][0] / ONE_HUNDRED)

    def sell_level(self, anchor: Decimal, step: int) -> Decimal:
        """The close at or over which ``step`` sells, in ``anchor`` mode."""
        return anchor * (Decimal(1) + self.steps[step][0] / ONE_HUNDRED)

    def decide(
        self,
        *,
        anchor: Decimal,
        close: Decimal,
        above: bool | None,
        held: Decimal,
        bought: Iterable[int] = (),
        sold: Iterable[int] = (),
        lots: Sequence[HeldLot] = (),
    ) -> list[Order]:
        """The orders this bar calls for, sells first.

        ``above`` is whether the bar closed above its trend average: ``None``
        when the average does not exist yet, which blocks buys and never
        triggers the exit. ``bought`` and ``sold`` are the steps already taken
        this cycle. ``lots`` is read only in ``lot`` mode.
        """
        bought = set(bought)
        sold = set(sold)
        orders: list[Order] = []

        if held > ZERO:
            if self.trend_filter and self.trend_exit and above is False:
                # Everything goes, so no take-profit or buy can apply this bar.
                return [Order(Side.SELL, REASON_TREND_EXIT, everything=True)]
            if self.mode == MODE_LOT:
                for lot in lots:
                    pct = self.steps[lot.step][0]
                    if close >= lot.mark * (Decimal(1) + pct / ONE_HUNDRED):
                        orders.append(
                            Order(Side.SELL, REASON_TAKE_PROFIT, step=lot.step, lot=lot.key)
                        )
                        bought.discard(lot.step)  # the step re-arms once its lot sells
            else:
                last = len(self.steps) - 1
                for level, (_, dollars) in enumerate(self.steps):
                    if level in sold or close < self.sell_level(anchor, level):
                        continue
                    orders.append(
                        Order(
                            Side.SELL,
                            REASON_TAKE_PROFIT,
                            step=level,
                            dollars=None if level == last else dollars,
                            everything=level == last,
                        )
                    )

        if self.trend_filter and above is not True:
            return orders
        for level, (_, dollars) in enumerate(self.steps):
            if level in bought or close > self.buy_level(anchor, level):
                continue
            orders.append(Order(Side.BUY, REASON_DIP, step=level, dollars=dollars))
        return orders


def parse_steps(text: str) -> tuple[tuple[Decimal, Decimal], ...]:
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


def describe_steps(steps: Sequence[tuple[Decimal, Decimal]]) -> str:
    return ", ".join(f"-{pct}%/${dollars}" for pct, dollars in steps)


def trend_bars(days: int, bar_interval_minutes: int) -> int:
    """How many bars make ``days`` of history."""
    return max(1, days * 24 * 60 // bar_interval_minutes)


def trend_average(candles: Sequence[Candle], bars: int) -> Decimal | None:
    """The mean of the last ``bars`` closes, the last bar included, or ``None``
    when there are fewer than ``bars``."""
    if bars <= 0:
        raise ValueError("the trend average needs at least one bar")
    if len(candles) < bars:
        return None
    return sum((c.close for c in candles[-bars:]), ZERO) / Decimal(bars)


def above_trend(candles: Sequence[Candle], bars: int) -> dict[datetime, bool]:
    """Per bar start: did it close above the average of the last ``bars`` closes?

    The average includes the bar itself. A bar with fewer than ``bars`` closes
    behind it has no entry, which :meth:`Ladder.decide` reads as "no average
    yet": no buy, and no exit.
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


def flat_anchor(candles: Sequence[Candle], *, since: datetime | None) -> Decimal | None:
    """The anchor while nothing is held: the highest close of a bar that ended
    after ``since``, or of every bar when ``since`` is ``None``."""
    closes = [c.close for c in candles if since is None or c.end > since]
    return max(closes) if closes else None


def trend_lookup(trend: Mapping[datetime, bool] | None, candle: Candle) -> bool | None:
    return None if trend is None else trend.get(candle.start)
