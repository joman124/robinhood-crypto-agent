"""Strategy behaviour: regimes, blending, and honest handling of thin data."""

import pytest

from robinhood_crypto_agent.config import StrategyConfig
from robinhood_crypto_agent.models import Direction, Regime, Signal
from robinhood_crypto_agent.strategy import CompositeStrategy, detect_regime
from robinhood_crypto_agent.strategy.base import SignalContext
from robinhood_crypto_agent.strategy.composite import empty_view
from robinhood_crypto_agent.strategy.signals import (
    BreakoutSignal,
    MeanReversionSignal,
    MomentumSignal,
    TrendSignal,
)
from tests.conftest import choppy, downtrend, make_candles, uptrend


@pytest.fixture
def strategy_config():
    return StrategyConfig()


def context(prices, config, observations=4):
    return SignalContext(
        symbol="BTC-USD",
        candles=make_candles(prices, observations=observations),
        config=config,
    )


class TestRegime:
    def test_uptrend_is_trending(self, strategy_config):
        assert detect_regime(context(uptrend(), strategy_config)).regime is Regime.TRENDING

    def test_chop_is_ranging(self, strategy_config):
        assert detect_regime(context(choppy(), strategy_config)).regime is Regime.RANGING

    def test_thin_history_is_unknown_not_ranging(self, strategy_config):
        """'No answer yet' and 'no trend' are different claims."""
        read = detect_regime(context(uptrend(6), strategy_config))
        assert read.regime is Regime.UNKNOWN
        assert "warm up" in read.rationale


class TestSignals:
    def test_trend_follows_direction(self, strategy_config):
        assert TrendSignal().evaluate(context(uptrend(), strategy_config)).score > 0
        assert TrendSignal().evaluate(context(downtrend(), strategy_config)).score < 0

    def test_mean_reversion_is_contrarian(self, strategy_config):
        """An overbought market is a sell for this source, by construction."""
        assert MeanReversionSignal().evaluate(context(uptrend(), strategy_config)).score < 0
        assert MeanReversionSignal().evaluate(context(downtrend(), strategy_config)).score > 0

    def test_breakout_fires_above_the_channel(self, strategy_config):
        prices = [100.0] * 30 + [140.0]
        signal = BreakoutSignal().evaluate(context(prices, strategy_config))
        assert signal.score > 0
        assert signal.direction is Direction.LONG

    def test_breakout_inside_the_channel_reports_reduced_confidence(self, strategy_config):
        inside = BreakoutSignal().evaluate(context([100.0] * 40, strategy_config))
        breaking = BreakoutSignal().evaluate(
            context([100.0] * 30 + [140.0], strategy_config)
        )
        assert inside.score == 0.0
        assert inside.confidence < breaking.confidence

    def test_momentum_reads_the_zscore(self, strategy_config):
        assert MomentumSignal().evaluate(context(uptrend(), strategy_config)).score > 0

    @pytest.mark.parametrize(
        "source", [TrendSignal(), MomentumSignal(), MeanReversionSignal(), BreakoutSignal()]
    )
    def test_every_source_declines_on_thin_history(self, source, strategy_config):
        """None means 'no opinion' -- never a neutral score papering over no data."""
        assert source.evaluate(context(uptrend(4), strategy_config)) is None

    @pytest.mark.parametrize(
        "source", [TrendSignal(), MomentumSignal(), MeanReversionSignal(), BreakoutSignal()]
    )
    def test_scores_stay_in_bounds(self, source, strategy_config):
        for prices in (uptrend(), downtrend(), choppy()):
            signal = source.evaluate(context(prices, strategy_config))
            assert -1.0 <= signal.score <= 1.0
            assert 0.0 <= signal.confidence <= 1.0


class TestComposite:
    def test_uptrend_produces_a_long(self, strategy_config):
        view = CompositeStrategy(strategy_config).evaluate(
            "BTC-USD", make_candles(uptrend())
        )
        assert view.direction is Direction.LONG
        assert view.regime is Regime.TRENDING
        assert view.score > 0

    def test_downtrend_produces_a_short(self, strategy_config):
        view = CompositeStrategy(strategy_config).evaluate(
            "BTC-USD", make_candles(downtrend())
        )
        assert view.direction is Direction.SHORT

    def test_thin_sampling_lowers_confidence(self, strategy_config):
        strategy = CompositeStrategy(strategy_config)
        rich = strategy.evaluate("BTC-USD", make_candles(uptrend(), observations=4))
        thin = strategy.evaluate("BTC-USD", make_candles(uptrend(), observations=1))
        assert thin.confidence < rich.confidence
        assert thin.confidence == pytest.approx(rich.confidence * 0.25, rel=0.05)

    def test_no_signals_means_zero_confidence_not_a_neutral_opinion(self, strategy_config):
        view = CompositeStrategy(strategy_config).evaluate("BTC-USD", make_candles(uptrend(4)))
        assert view.confidence == 0.0
        assert view.direction is Direction.FLAT
        assert view.signals == []

    def test_unweighted_sources_are_inert(self, strategy_config):
        """Adding a source cannot change the blend until it is given a weight."""

        class Rogue:
            name = "rogue"

            def evaluate(self, ctx):
                return Signal(
                    name="rogue",
                    symbol=ctx.symbol,
                    score=-1.0,
                    confidence=1.0,
                    direction=Direction.SHORT,
                    rationale="maximally bearish",
                )

        candles = make_candles(uptrend())
        baseline = CompositeStrategy(strategy_config).evaluate("BTC-USD", candles)
        with_rogue = CompositeStrategy(
            strategy_config,
            sources=[TrendSignal(), MomentumSignal(), MeanReversionSignal(), BreakoutSignal(), Rogue()],
        ).evaluate("BTC-USD", candles)
        assert with_rogue.score == pytest.approx(baseline.score)

    def test_missing_sources_drag_confidence_down(self, strategy_config):
        candles = make_candles(uptrend())
        full = CompositeStrategy(strategy_config).evaluate("BTC-USD", candles)
        partial = CompositeStrategy(strategy_config, sources=[TrendSignal()]).evaluate(
            "BTC-USD", candles
        )
        assert partial.confidence < full.confidence

    def test_regime_changes_the_weights(self, strategy_config):
        strategy = CompositeStrategy(strategy_config)
        trending = strategy.evaluate("BTC-USD", make_candles(uptrend()))
        ranging = strategy.evaluate("BTC-USD", make_candles(choppy()))
        assert trending.weights["trend"] > trending.weights["mean_reversion"]
        assert ranging.weights["mean_reversion"] > ranging.weights["trend"]


def test_empty_view_helper():
    view = empty_view("btcusd", "no data")
    assert view.symbol == "BTC-USD"
    assert view.direction is Direction.FLAT
    assert view.notes == ["no data"]


def test_signal_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        Signal("x", "BTC-USD", 2.0, 0.5, Direction.LONG, "")
    with pytest.raises(ValueError):
        Signal("x", "BTC-USD", 0.5, 1.5, Direction.LONG, "")
