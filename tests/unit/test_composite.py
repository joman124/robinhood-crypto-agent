from __future__ import annotations

from robinhood_crypto_agent.config import RegimeConfig, StrategyWeights
from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.composite import CompositeStrategy, default_sources
from tests.fixtures.helpers import make_flat_ohlcv, make_trending_ohlcv


class FakeSource:
    def __init__(self, name: str, value: float, confidence: float):
        self.name = name
        self._value = value
        self._confidence = confidence

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        return SignalScore(source=self.name, value=self._value, confidence=self._confidence)


def _weights(threshold: float = 0.3, min_confidence: float = 0.3) -> StrategyWeights:
    return StrategyWeights(
        regimes={
            "trending": {"a": 1.0, "b": 1.0},
            "ranging": {"a": 1.0, "b": 1.0},
        },
        signal_threshold=threshold,
        min_confidence=min_confidence,
        regime=RegimeConfig(),
    )


def test_no_signal_when_below_magnitude_threshold():
    sources = [FakeSource("a", 0.1, 0.9), FakeSource("b", 0.1, 0.9)]
    strategy = CompositeStrategy(sources, _weights(threshold=0.5))
    df = make_flat_ohlcv(n=80)
    assert strategy.generate_signal(MarketSnapshot(symbol="BTC", candles=df)) is None


def test_no_signal_when_confidence_too_low():
    sources = [FakeSource("a", 0.9, 0.1), FakeSource("b", 0.9, 0.1)]
    strategy = CompositeStrategy(sources, _weights(min_confidence=0.5))
    df = make_flat_ohlcv(n=80)
    assert strategy.generate_signal(MarketSnapshot(symbol="BTC", candles=df)) is None


def test_buy_signal_when_sources_agree_bullish():
    sources = [FakeSource("a", 0.8, 0.9), FakeSource("b", 0.7, 0.9)]
    strategy = CompositeStrategy(sources, _weights())
    df = make_flat_ohlcv(n=80)
    signal = strategy.generate_signal(MarketSnapshot(symbol="BTC", candles=df))
    assert signal is not None
    assert signal.side.value == "buy"
    assert signal.composite_value > 0


def test_sell_signal_when_sources_agree_bearish():
    sources = [FakeSource("a", -0.8, 0.9), FakeSource("b", -0.7, 0.9)]
    strategy = CompositeStrategy(sources, _weights())
    df = make_flat_ohlcv(n=80)
    signal = strategy.generate_signal(MarketSnapshot(symbol="BTC", candles=df))
    assert signal is not None
    assert signal.side.value == "sell"


def test_regime_weighting_changes_which_source_dominates():
    weights = StrategyWeights(
        regimes={"trending": {"a": 1.0, "b": 0.0}, "ranging": {"a": 0.0, "b": 1.0}},
        signal_threshold=0.3,
        min_confidence=0.3,
        regime=RegimeConfig(),
    )
    sources = [FakeSource("a", 0.9, 0.9), FakeSource("b", -0.9, 0.9)]
    strategy = CompositeStrategy(sources, weights)

    trending_df = make_trending_ohlcv(n=80, daily_return=0.02, noise=0.0)
    signal = strategy.generate_signal(MarketSnapshot(symbol="BTC", candles=trending_df))
    assert signal is not None
    assert signal.composite_value > 0  # dominated by source "a", weighted only in TRENDING


def test_default_sources_produce_a_buy_signal_on_strongly_trending_data():
    weights = StrategyWeights(
        regimes={
            "trending": {
                "trend": 0.35,
                "mean_reversion": 0.10,
                "volume": 0.20,
                "relative_strength": 0.25,
                "sentiment": 0.10,
            },
            "ranging": {
                "trend": 0.10,
                "mean_reversion": 0.35,
                "volume": 0.15,
                "relative_strength": 0.15,
                "sentiment": 0.25,
            },
        },
        signal_threshold=0.1,
        min_confidence=0.1,
        regime=RegimeConfig(),
    )
    strategy = CompositeStrategy(default_sources(), weights)

    df = make_trending_ohlcv(n=90, daily_return=0.03, noise=0.0, start_price=100)
    snapshot = MarketSnapshot(symbol="BTC", candles=df, watchlist_candles={"BTC": df})
    signal = strategy.generate_signal(snapshot)

    assert signal is not None
    assert signal.side.value == "buy"
    assert len(signal.component_scores) == len(default_sources())
