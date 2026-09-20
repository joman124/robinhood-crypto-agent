from __future__ import annotations

from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.signals.mean_reversion import MeanReversionSignal
from robinhood_crypto_agent.strategy.signals.relative_strength import RelativeStrengthSignal
from robinhood_crypto_agent.strategy.signals.sentiment import SentimentSignal
from robinhood_crypto_agent.strategy.signals.trend import TrendSignal
from robinhood_crypto_agent.strategy.signals.volume import VolumeSignal
from tests.fixtures.helpers import (
    make_flat_ohlcv,
    make_linear_trend_ohlcv,
    make_oscillating_ohlcv,
    make_trending_ohlcv,
)


def _snapshot(symbol: str, candles, **kwargs) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, candles=candles, **kwargs)


class TestTrendSignal:
    def test_bullish_in_strong_uptrend(self):
        # Constant-dollar-delta trend (not a compounding %) so the MACD
        # histogram's sign isn't muddied by deceleration-in-$-terms
        # artifacts a compounding series produces.
        df = make_linear_trend_ohlcv(n=80, start_price=100, daily_delta=5)
        score = TrendSignal().score(_snapshot("BTC", df))
        assert score.value > 0
        assert score.confidence > 0

    def test_bearish_in_strong_downtrend(self):
        df = make_linear_trend_ohlcv(n=80, start_price=10_000, daily_delta=-50)
        score = TrendSignal().score(_snapshot("BTC", df))
        assert score.value < 0

    def test_no_opinion_with_insufficient_history(self):
        df = make_trending_ohlcv(n=10, daily_return=0.02, noise=0.0)
        score = TrendSignal().score(_snapshot("BTC", df))
        assert score.value == 0.0
        assert score.confidence == 0.0


class TestMeanReversionSignal:
    def test_bullish_when_oversold(self):
        df = make_oscillating_ohlcv(n=90, amplitude=25, period=30)
        signal_source = MeanReversionSignal()
        # Walk forward and find a point where RSI is oversold to assert the
        # sign of the signal there, rather than assuming the final bar.
        found_oversold = False
        for i in range(20, len(df)):
            score = signal_source.score(_snapshot("BTC", df.iloc[: i + 1]))
            if "oversold" in score.rationale:
                assert score.value > 0
                found_oversold = True
        assert found_oversold, "expected the oscillating fixture to hit an oversold RSI reading"

    def test_bearish_when_overbought(self):
        df = make_oscillating_ohlcv(n=90, amplitude=25, period=30)
        signal_source = MeanReversionSignal()
        found_overbought = False
        for i in range(20, len(df)):
            score = signal_source.score(_snapshot("BTC", df.iloc[: i + 1]))
            if "overbought" in score.rationale:
                assert score.value < 0
                found_overbought = True
        assert found_overbought, "expected the oscillating fixture to hit an overbought RSI reading"


class TestVolumeSignal:
    def test_no_opinion_with_insufficient_history(self):
        df = make_flat_ohlcv(n=5)
        score = VolumeSignal().score(_snapshot("BTC", df))
        assert score.value == 0.0
        assert score.confidence == 0.0

    def test_confirms_uptrend_with_rising_volume(self):
        df = make_trending_ohlcv(n=60, daily_return=0.02, noise=0.0)
        df["volume"] = [1000.0 + i * 50 for i in range(len(df))]  # rising volume alongside price
        score = VolumeSignal().score(_snapshot("BTC", df))
        assert score.value > 0


class TestRelativeStrengthSignal:
    def test_no_opinion_without_peer_data(self):
        df = make_flat_ohlcv(n=60)
        score = RelativeStrengthSignal().score(_snapshot("BTC", df))
        assert score.value == 0.0
        assert score.confidence == 0.0

    def test_ranks_strongest_symbol_positively(self):
        strong = make_trending_ohlcv(n=60, daily_return=0.03, noise=0.0, seed=1)
        weak = make_trending_ohlcv(n=60, daily_return=0.001, noise=0.0, seed=2)
        peers = {"BTC": strong, "ETH": weak, "SOL": weak}

        score = RelativeStrengthSignal().score(
            _snapshot("BTC", strong, watchlist_candles=peers)
        )
        assert score.value > 0

        weak_score = RelativeStrengthSignal().score(
            _snapshot("ETH", weak, watchlist_candles=peers)
        )
        assert weak_score.value < score.value


class TestSentimentSignal:
    def test_no_opinion_without_data(self):
        df = make_flat_ohlcv(n=10)
        score = SentimentSignal().score(_snapshot("BTC", df, fear_greed_index=None))
        assert score.value == 0.0
        assert score.confidence == 0.0

    def test_bullish_on_extreme_fear(self):
        df = make_flat_ohlcv(n=10)
        score = SentimentSignal().score(_snapshot("BTC", df, fear_greed_index=5.0))
        assert score.value > 0

    def test_bearish_on_extreme_greed(self):
        df = make_flat_ohlcv(n=10)
        score = SentimentSignal().score(_snapshot("BTC", df, fear_greed_index=95.0))
        assert score.value < 0

    def test_neutral_in_the_middle(self):
        df = make_flat_ohlcv(n=10)
        score = SentimentSignal().score(_snapshot("BTC", df, fear_greed_index=50.0))
        assert score.value == 0.0
