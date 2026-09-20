"""The contract every signal source implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Protocol, Sequence, runtime_checkable

from .. import indicators
from ..config import StrategyConfig
from ..models import Candle, Signal


@dataclass
class SignalContext:
    """Everything a signal source is allowed to see.

    Indicators are computed lazily and cached, so four signal sources sharing a
    context compute each series once rather than four times -- and, more
    importantly, all four see *identical* values rather than independently
    recomputed ones that could drift if a parameter were passed inconsistently.
    """

    symbol: str
    candles: Sequence[Candle]
    config: StrategyConfig
    notes: list[str] = field(default_factory=list)

    @cached_property
    def closes(self) -> list[float]:
        return indicators.closes(self.candles)

    @cached_property
    def rsi(self) -> list[float | None]:
        return indicators.rsi(self.closes, self.config.rsi_period)

    @cached_property
    def macd(self) -> tuple[list[float | None], list[float | None], list[float | None]]:
        return indicators.macd(
            self.closes, self.config.fast_ma, self.config.slow_ma, self.config.signal_ma
        )

    @cached_property
    def atr(self) -> list[float | None]:
        return indicators.atr(self.candles, self.config.atr_period)

    @cached_property
    def adx(self) -> tuple[list[float | None], list[float | None], list[float | None]]:
        return indicators.adx(self.candles, self.config.adx_period)

    @cached_property
    def bollinger(self) -> tuple[list[float | None], list[float | None], list[float | None]]:
        return indicators.bollinger(self.closes, self.config.breakout_lookback)

    @cached_property
    def donchian(self) -> tuple[list[float | None], list[float | None]]:
        return indicators.donchian(self.candles, self.config.breakout_lookback)

    @cached_property
    def sample_quality(self) -> float:
        """How well-sampled the bars are, in [0, 1].

        A bar built from one tick is a point, not a bar. Since bars here are
        aggregated from polled quotes rather than delivered by an exchange feed,
        every signal's confidence is scaled by this -- thin sampling produces
        weak opinions rather than confident ones built on nothing.
        """
        if not self.candles:
            return 0.0
        average = sum(c.observations for c in self.candles) / len(self.candles)
        # 4+ observations per bar is treated as fully sampled.
        return min(1.0, average / 4.0)

    def history_factor(self, needed: int) -> float:
        """How much of the history a signal wants actually exists, in [0, 1]."""
        if needed <= 0:
            return 1.0
        return min(1.0, len(self.candles) / needed)


@runtime_checkable
class SignalSource(Protocol):
    """A named source of one bounded opinion about one symbol."""

    name: str

    def evaluate(self, context: SignalContext) -> Signal | None:
        """Return a signal, or ``None`` when there is not enough data.

        Returning ``None`` is the honest answer to thin history. A source must
        never return a neutral score to paper over missing data -- the composite
        distinguishes "no opinion" from "an opinion of zero".
        """
        ...
