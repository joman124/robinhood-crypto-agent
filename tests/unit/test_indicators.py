"""Indicator tests.

RSI is checked against Wilder's published worked example. The rest are checked
against hand-computed values and against structural properties (alignment,
warm-up length) that are the usual source of silent indicator bugs.
"""

import math

import pytest

from robinhood_crypto_agent import indicators
from tests.conftest import make_candles

# Wilder, "New Concepts in Technical Trading Systems".
WILDER = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


def test_rsi_matches_wilders_worked_example():
    series = indicators.rsi(WILDER, 14)
    assert series[14] == pytest.approx(70.4641, abs=1e-3)


def test_rsi_smoothing_recursion_is_wilders_not_a_plain_ema():
    """Step 15 is (avg*13 + new)/14 -- verified by hand from the source data."""
    series = indicators.rsi(WILDER, 14)
    assert series[15] == pytest.approx(66.2496, abs=1e-3)


def test_rsi_warmup_is_none_and_output_is_aligned():
    series = indicators.rsi(WILDER, 14)
    assert len(series) == len(WILDER)
    assert series[:14] == [None] * 14


def test_rsi_saturates_without_dividing_by_zero():
    assert indicators.rsi([float(i) for i in range(20)], 14)[-1] == 100.0
    flat = indicators.rsi([5.0] * 20, 14)[-1]
    assert flat == 50.0  # no movement either way is neutral, not 100


def test_sma_and_ema_are_aligned_and_correct():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    sma = indicators.sma(values, 3)
    assert sma == [None, None, 2.0, 3.0, 4.0]
    ema = indicators.ema(values, 3)
    assert ema[2] == pytest.approx(2.0)  # seeded with the SMA
    assert ema[3] == pytest.approx(3.0)


def test_macd_components_are_all_aligned():
    values = [100 + i for i in range(60)]
    macd, signal, histogram = indicators.macd(values, 12, 26, 9)
    assert len(macd) == len(signal) == len(histogram) == len(values)
    index = next(i for i, v in enumerate(histogram) if v is not None)
    assert histogram[index] == pytest.approx(macd[index] - signal[index])


def test_macd_rejects_fast_slower_than_slow():
    with pytest.raises(ValueError):
        indicators.macd([1.0] * 40, 26, 12, 9)


def test_true_range_uses_the_previous_close():
    candles = make_candles([100, 110])
    ranges = indicators.true_ranges(candles)
    assert ranges[0] == pytest.approx(float(candles[0].high - candles[0].low))
    assert ranges[1] == pytest.approx(
        max(
            float(candles[1].high - candles[1].low),
            abs(float(candles[1].high) - 100.0),
            abs(float(candles[1].low) - 100.0),
        )
    )


def test_adx_is_high_in_a_trend_and_low_in_chop():
    trend = indicators.adx(make_candles([100 * 1.01**i for i in range(60)]), 14)[0]
    chop = indicators.adx(make_candles([100 + 5 * math.sin(i / 2.4) for i in range(60)]), 14)[0]
    assert indicators.latest(trend) > 40
    assert indicators.latest(chop) < 30


def test_adx_returns_empty_series_when_too_short():
    adx, plus, minus = indicators.adx(make_candles([100.0] * 10), 14)
    assert indicators.latest(adx) is None
    assert len(adx) == len(plus) == len(minus) == 10


def test_directional_indicators_point_the_right_way():
    _, plus, minus = indicators.adx(make_candles([100 * 1.01**i for i in range(60)]), 14)
    assert indicators.latest(plus) > indicators.latest(minus)
    _, plus_down, minus_down = indicators.adx(
        make_candles([100 * 0.99**i for i in range(60)]), 14
    )
    assert indicators.latest(minus_down) > indicators.latest(plus_down)


def test_donchian_excludes_the_current_bar():
    """Otherwise a bar that makes a new high could never breach the channel."""
    prices = [100.0] * 20 + [150.0]
    candles = make_candles(prices)
    upper, lower = indicators.donchian(candles, 20)
    assert upper[20] == pytest.approx(float(candles[0].high))
    assert float(candles[20].close) > upper[20]


def test_bollinger_bands_bracket_the_mean():
    values = [100 + math.sin(i) for i in range(40)]
    upper, middle, lower = indicators.bollinger(values, 20, 2.0)
    assert lower[-1] < middle[-1] < upper[-1]


def test_realized_volatility_and_zscore():
    assert indicators.realized_volatility([100.0] * 30, 20) == pytest.approx(0.0)
    assert indicators.realized_volatility([1.0] * 5, 20) is None
    assert indicators.zscore([1.0] * 20, 20) == 0.0
    rising = indicators.zscore([float(i) for i in range(20)], 20)
    assert rising > 1.0


def test_latest_and_clamp():
    assert indicators.latest([None, 1.0, None]) == 1.0
    assert indicators.latest([None, None]) is None
    assert indicators.clamp(5.0) == 1.0
    assert indicators.clamp(-5.0) == -1.0
