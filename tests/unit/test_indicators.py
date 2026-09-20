from __future__ import annotations

import pandas as pd
import pytest

from robinhood_crypto_agent.strategy import indicators
from tests.fixtures.helpers import make_flat_ohlcv, make_oscillating_ohlcv, make_trending_ohlcv


def test_sma_matches_manual_average():
    series = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    result = indicators.sma(series, window=3)
    assert result.iloc[2] == pytest.approx(2.0)  # mean(1,2,3)
    assert result.iloc[5] == pytest.approx(5.0)  # mean(4,5,6)
    assert result.iloc[:2].isna().all()


def test_rsi_is_100_for_strictly_increasing_series():
    series = pd.Series(range(1, 30), dtype=float)
    result = indicators.rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_is_0_for_strictly_decreasing_series():
    series = pd.Series(range(30, 1, -1), dtype=float)
    result = indicators.rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_is_neutral_for_flat_series():
    series = pd.Series([100.0] * 30)
    result = indicators.rsi(series, period=14)
    # No gains or losses at all (avg_gain == avg_loss == 0) is neutral, not
    # "maximally overbought" - distinguishing this from the legitimate
    # "all gains, no losses" case (avg_gain > 0, avg_loss == 0) is what
    # keeps a flat/no-data market from misreporting as an extreme signal.
    assert result.iloc[-1] == pytest.approx(50.0)


def test_macd_histogram_positive_when_fast_above_slow_in_uptrend():
    df = make_trending_ohlcv(n=80, daily_return=0.02, noise=0.0)
    _, _, histogram = indicators.macd(df["close"])
    assert histogram.iloc[-1] > 0


def test_adx_high_for_strong_trend_low_for_ranging():
    trending = make_trending_ohlcv(n=80, daily_return=0.02, noise=0.0)
    # A small-amplitude oscillation (real price movement, no sustained
    # trend) rather than a perfectly flat series - a perfectly flat series
    # has zero directional movement at all, which makes ADX mathematically
    # undefined (NaN) rather than merely low.
    ranging = make_oscillating_ohlcv(n=80, amplitude=1, period=10)

    trending_adx = indicators.adx(trending, period=14).iloc[-1]
    ranging_adx = indicators.adx(ranging, period=14).iloc[-1]

    assert trending_adx > 25
    assert pd.notna(ranging_adx)
    assert ranging_adx < trending_adx


def test_atr_zero_for_flat_series():
    flat = make_flat_ohlcv(n=40)
    result = indicators.atr(flat, period=14)
    assert result.iloc[-1] == pytest.approx(0.0, abs=0.5)


def test_obv_increases_on_up_days_decreases_on_down_days():
    df = make_trending_ohlcv(n=40, daily_return=0.01, noise=0.0)
    result = indicators.obv(df)
    assert result.iloc[-1] > result.iloc[0]


def test_momentum_matches_pct_change():
    series = pd.Series([100.0, 110.0, 121.0])
    result = indicators.momentum(series, window=2)
    assert result.iloc[2] == pytest.approx(0.21)


def test_oscillating_series_produces_rsi_extremes():
    df = make_oscillating_ohlcv(n=90, amplitude=25, period=30)
    result = indicators.rsi(df["close"], period=14)
    assert result.max() > 65
    assert result.min() < 35
