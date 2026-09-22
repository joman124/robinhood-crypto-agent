"""Blending the signal sources into one opinion per symbol.

The blend is a confidence-weighted average under regime-dependent weights:

    score      = sum(w_i * c_i * s_i) / sum(w_i * c_i)
    confidence = sum(w_i * c_i) / sum(w_i over ALL configured sources)

The denominators differ on purpose. The score averages over the signals that
*did* report, so an unavailable signal does not drag the score toward zero. The
confidence divides by the total configured weight including signals that
reported nothing -- so if three of four sources have no history, the composite
says so with a low confidence rather than presenting one source's reading as
the settled view.

The one exception is an *optional* source (news): its silence is the normal
state, not missing data, so a silent optional source is dropped from the
weights entirely. With no recent news, the blend is exactly the price-only one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import StrategyConfig
from ..models import Candle, CompositeView, Direction, NewsItem, Regime, Signal
from ..symbols import canonical
from .base import SignalContext, SignalSource
from .regime import detect_regime
from .signals import default_signal_sources

#: Below this magnitude the blended score is treated as no directional opinion.
DIRECTION_DEADBAND = 0.05


@dataclass
class CompositeStrategy:
    """Runs every signal source and blends the results."""

    config: StrategyConfig
    sources: Sequence[SignalSource] | None = None

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = default_signal_sources()

    def evaluate(
        self, symbol: str, candles: Sequence[Candle], news: Sequence[NewsItem] = ()
    ) -> CompositeView:
        """Produce the blended view for one symbol."""
        symbol = canonical(symbol)
        context = SignalContext(symbol=symbol, candles=candles, config=self.config, news=news)
        notes: list[str] = []

        if len(candles) < self.config.min_bars:
            notes.append(
                f"only {len(candles)} bars available, {self.config.min_bars} configured as the "
                "minimum -- signals are reported but confidence is reduced accordingly"
            )

        regime_read = detect_regime(context)
        notes.append(f"regime: {regime_read.rationale}")

        weights = self.config.weights_for(regime_read.regime.value)

        signals: list[Signal] = []
        for source in self.sources or []:
            signal = source.evaluate(context)
            if signal is None:
                if getattr(source, "optional", False):
                    weights.pop(source.name, None)
                else:
                    notes.append(f"{source.name}: insufficient history, no opinion")
                continue
            signals.append(signal)

        score, confidence = self._blend(signals, weights)
        direction = _direction_for(score)

        if context.sample_quality < 1.0:
            average = (
                sum(c.observations for c in candles) / len(candles) if candles else 0.0
            )
            notes.append(
                f"bars average {average:.1f} observations each; confidence scaled by "
                f"{context.sample_quality:.2f} for thin sampling"
            )

        return CompositeView(
            symbol=symbol,
            regime=regime_read.regime,
            score=score,
            confidence=confidence,
            direction=direction,
            signals=signals,
            weights={name: round(weight, 4) for name, weight in weights.items()},
            notes=notes,
        )

    def _blend(
        self, signals: Sequence[Signal], weights: dict[str, float]
    ) -> tuple[float, float]:
        total_configured = sum(weights.values())
        if total_configured <= 0:
            return 0.0, 0.0

        effective = 0.0
        weighted_score = 0.0
        for signal in signals:
            weight = weights.get(signal.name)
            if weight is None:
                # A source with no configured weight is inert rather than
                # implicitly equal-weighted, so adding a source cannot change
                # the blend until it is given a weight deliberately.
                continue
            contribution = weight * signal.confidence
            effective += contribution
            weighted_score += contribution * signal.score

        if effective <= 0:
            return 0.0, 0.0

        score = max(-1.0, min(1.0, weighted_score / effective))
        confidence = max(0.0, min(1.0, effective / total_configured))
        return score, confidence


def _direction_for(score: float) -> Direction:
    if score > DIRECTION_DEADBAND:
        return Direction.LONG
    if score < -DIRECTION_DEADBAND:
        return Direction.SHORT
    return Direction.FLAT


def empty_view(symbol: str, reason: str) -> CompositeView:
    """A view for a symbol that could not be evaluated at all."""
    return CompositeView(
        symbol=canonical(symbol),
        regime=Regime.UNKNOWN,
        score=0.0,
        confidence=0.0,
        direction=Direction.FLAT,
        signals=[],
        weights={},
        notes=[reason],
    )
