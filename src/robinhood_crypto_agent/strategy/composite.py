from __future__ import annotations

from robinhood_crypto_agent.config import StrategyWeights
from robinhood_crypto_agent.models import Side, Signal, SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot, SignalSource
from robinhood_crypto_agent.strategy.regime import detect_regime


class CompositeStrategy:
    """Blends independent SignalSources using per-regime weights.

    Each source's contribution is weighted by both its configured per-regime
    weight and its own confidence, so a source with nothing to say (low
    confidence) doesn't drag the composite toward zero. A Signal is only
    produced if the resulting composite clears both a minimum magnitude and
    a minimum average confidence - see config/strategy_weights.yaml.
    """

    def __init__(self, sources: list[SignalSource], weights: StrategyWeights):
        self.sources = sources
        self.weights = weights

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal | None:
        regime = detect_regime(
            snapshot.candles,
            adx_period=self.weights.regime.adx_period,
            adx_trending_threshold=self.weights.regime.adx_trending_threshold,
        )
        regime_weights = self.weights.regimes.get(regime.value, {})

        scores: list[SignalScore] = [source.score(snapshot) for source in self.sources]

        weighted_sum = 0.0
        weight_total = 0.0
        confidence_sum = 0.0
        confidence_count = 0

        for score in scores:
            weight = regime_weights.get(score.source, 0.0)
            if weight <= 0 or score.confidence <= 0:
                continue
            effective_weight = weight * score.confidence
            weighted_sum += effective_weight * score.value
            weight_total += effective_weight
            confidence_sum += score.confidence
            confidence_count += 1

        if weight_total == 0 or confidence_count == 0:
            return None

        composite_value = weighted_sum / weight_total
        composite_confidence = confidence_sum / confidence_count

        if (
            abs(composite_value) < self.weights.signal_threshold
            or composite_confidence < self.weights.min_confidence
        ):
            return None

        side = Side.BUY if composite_value > 0 else Side.SELL
        return Signal(
            symbol=snapshot.symbol,
            regime=regime,
            composite_value=composite_value,
            composite_confidence=composite_confidence,
            side=side,
            component_scores=scores,
        )


def default_sources() -> list[SignalSource]:
    """The default Phase 1 signal set (docs/strategy.md)."""
    from robinhood_crypto_agent.strategy.signals.mean_reversion import MeanReversionSignal
    from robinhood_crypto_agent.strategy.signals.relative_strength import (
        RelativeStrengthSignal,
    )
    from robinhood_crypto_agent.strategy.signals.sentiment import SentimentSignal
    from robinhood_crypto_agent.strategy.signals.trend import TrendSignal
    from robinhood_crypto_agent.strategy.signals.volume import VolumeSignal

    return [
        TrendSignal(),
        MeanReversionSignal(),
        VolumeSignal(),
        RelativeStrengthSignal(),
        SentimentSignal(),
    ]
