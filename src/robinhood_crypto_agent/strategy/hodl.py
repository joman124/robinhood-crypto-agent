"""The long-term sleeve: buy in tranches, then hold. It never sells.

Holding has only one decision -- when to buy -- so the sleeve comes in three
ways of making it, compared side by side in ``rhca backtest --strategies
split``:

- **lump**: the sleeve's money goes in on the first day, split evenly across
  the coins. This is the ``hold (equal weight)`` baseline.
- **dca**: one tranche of each coin every week until all ten are bought.
  A sleeve too small for ten tranches at the minimum trade buys fewer,
  minimum-sized ones (see :meth:`Accumulate.tranches_for`); so does ``dip``.
- **dip** ("buy low"): one tranche at most once a week, and only on a daily
  close *under* the coin's 200-day average. While a coin trades above its
  average nothing is bought and the cash waits, however long that is.

The 200-day average is the usual line between "cheap" and "dear" for a coin
that trends: under it, the price is below where it has spent most of the last
seven months. Whether waiting for it pays better than buying at once is what
the backtest answers -- in a market that only rises, "dip" never buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..models import Candle
from ..numeric import ZERO

LUMP = "lump"
DCA = "dca"
DIP = "dip"
MODES = (DIP, DCA, LUMP)

LABELS = {
    DIP: "buy low",
    DCA: "weekly DCA",
    LUMP: "lump sum",
}


@dataclass(frozen=True)
class Accumulate:
    """When the long-term sleeve buys. Fixed before any backtest result was seen."""

    mode: str = DIP
    #: How many equal buys each coin's share is split into (``lump`` uses one).
    tranches: int = 10
    #: The least time between two buys of the same coin.
    every_days: int = 7
    #: ``dip`` buys only on a close under the mean of this many daily closes.
    average_days: int = 200

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"the long-term mode must be one of {', '.join(MODES)}")
        if min(self.tranches, self.every_days, self.average_days) < 1:
            raise ValueError("tranches, every_days and average_days must be at least 1")

    @property
    def label(self) -> str:
        return LABELS[self.mode]

    @property
    def tranche_count(self) -> int:
        return 1 if self.mode == LUMP else self.tranches

    def tranches_for(self, share: Decimal, min_trade: Decimal) -> int:
        """How many buys a coin's ``share`` is split into: ``tranche_count``,
        or -- when that would make each one smaller than the minimum trade --
        as many minimum-sized ones as fit, and at least one."""
        count = self.tranche_count
        if min_trade > ZERO and share / count < min_trade:
            count = max(1, int(share // min_trade))
        return count

    def due(
        self,
        *,
        day: datetime,
        close: Decimal,
        average: Decimal | None,
        last_buy: datetime | None,
    ) -> bool:
        """Whether a coin that still has tranches to buy buys one today."""
        if last_buy is not None and day - last_buy < timedelta(days=self.every_days):
            return False
        if self.mode == DIP:
            return average is not None and close < average
        return True

    def describe(self, tranches: int | None = None) -> str:
        """``tranches``: the count :meth:`tranches_for` settled on, if not ``tranches``."""
        if self.mode == LUMP:
            return "Buy every coin's share on the first day, and hold it."
        count = tranches or self.tranches
        cadence = f"one of {count} equal tranches of each coin at most every {self.every_days} days"
        if self.mode == DCA:
            return f"Buy {cadence}, whatever the price, and hold them."
        return (
            f"Buy {cadence}, only on a daily close under its {self.average_days}-day average, "
            "and hold them. Cash not yet spent waits."
        )


def moving_averages(daily: Sequence[Candle], days: int) -> dict[datetime, Decimal]:
    """The mean of the last ``days`` daily closes, today included, keyed by each
    day's start. Days without that much history behind them are left out."""
    closes = [bar.close for bar in daily]
    averages: dict[datetime, Decimal] = {}
    total = ZERO
    for index, close in enumerate(closes):
        total += close
        if index >= days:
            total -= closes[index - days]
        if index >= days - 1:
            averages[daily[index].start] = total / Decimal(days)
    return averages
