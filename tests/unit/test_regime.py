from __future__ import annotations

from robinhood_crypto_agent.models import Regime
from robinhood_crypto_agent.strategy.regime import detect_regime
from tests.fixtures.helpers import make_flat_ohlcv, make_trending_ohlcv


def test_trending_series_detected_as_trending():
    df = make_trending_ohlcv(n=80, daily_return=0.02, noise=0.0)
    assert detect_regime(df) is Regime.TRENDING


def test_flat_series_detected_as_ranging():
    df = make_flat_ohlcv(n=80)
    assert detect_regime(df) is Regime.RANGING


def test_insufficient_history_defaults_to_ranging():
    df = make_trending_ohlcv(n=5, daily_return=0.02, noise=0.0)
    assert detect_regime(df) is Regime.RANGING


def test_threshold_is_respected():
    df = make_trending_ohlcv(n=80, daily_return=0.02, noise=0.0)
    # ADX is bounded at 100, so a threshold above that can never be cleared -
    # proves the threshold parameter is actually used, regardless of how
    # strong the underlying trend is.
    assert detect_regime(df, adx_trending_threshold=150.0) is Regime.RANGING
