"""Technical indicators over a bar series.

Two conventions run through this module:

1. **Every series returned is the same length as its input**, padded with
   ``None`` through the warm-up period. Index ``i`` of any output always lines
   up with bar ``i``. Returning only the valid tail is how indicators get
   silently misaligned by one bar against each other.
2. **Indicators are floats, prices are Decimals.** An indicator is a statistic,
   not a monetary amount, so float is the right type; but nothing here feeds a
   float back into an order field -- prices used for orders are re-read from
   the Decimal source.

Smoothing follows Wilder where Wilder defined it (RSI, ATR, ADX), which is what
charting packages show; using a plain EMA instead produces values that look
almost right and disagree with every other tool.
"""

from __future__ import annotations

import math
from typing import Sequence

from .models import Candle

Series = list[float | None]


def _as_floats(values: Sequence[object]) -> list[float]:
    return [float(v) for v in values]  # type: ignore[arg-type]


def closes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def latest(series: Sequence[float | None]) -> float | None:
    """The last non-``None`` value, or ``None`` if the series never warmed up."""
    for value in reversed(series):
        if value is not None:
            return value
    return None


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError("period must be at least 1")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, seeded with the first full SMA."""
    if period < 1:
        raise ValueError("period must be at least 1")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = (values[i] - current) * multiplier + current
        out[i] = current
    return out


def wilder_smooth(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing: an EMA with alpha = 1/period, seeded with the mean."""
    out: Series = [None] * len(values)
    if len(values) < period or period < 1:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = current + (values[i] - current) / period
        out[i] = current
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Wilder's Relative Strength Index, in [0, 100]."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        # No downside in the window: RSI is 100 by definition (or 50 if flat).
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """MACD line, signal line, and histogram."""
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    macd_line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_line, slow_line, strict=True)
    ]

    # The signal line is an EMA of the MACD line, which only exists from the
    # slow period onward -- so it is computed over the dense tail and re-padded.
    dense = [v for v in macd_line if v is not None]
    signal_dense = ema(dense, signal)
    signal_line: Series = [None] * len(values)
    histogram: Series = [None] * len(values)
    offset = len(macd_line) - len(dense)
    for i, value in enumerate(signal_dense):
        if value is None:
            continue
        index = offset + i
        signal_line[index] = value
        macd_value = macd_line[index]
        if macd_value is not None:
            histogram[index] = macd_value - value
    return macd_line, signal_line, histogram


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    """True range per bar. The first bar uses its own high-low."""
    out: list[float] = []
    for i, candle in enumerate(candles):
        high, low = float(candle.high), float(candle.low)
        if i == 0:
            out.append(high - low)
            continue
        previous_close = float(candles[i - 1].close)
        out.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    """Average True Range (Wilder)."""
    if not candles:
        return []
    return wilder_smooth(true_ranges(candles), period)


def adx(
    candles: Sequence[Candle], period: int = 14
) -> tuple[Series, Series, Series]:
    """Average Directional Index with +DI and -DI (Wilder).

    ADX measures trend *strength* without direction, which is exactly what the
    regime detector needs: it answers "is this trending?" separately from
    "which way?".
    """
    size = len(candles)
    empty: Series = [None] * size
    if size < period * 2:
        return empty, list(empty), list(empty)

    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, size):
        up_move = float(candles[i].high) - float(candles[i - 1].high)
        down_move = float(candles[i - 1].low) - float(candles[i].low)
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr = true_ranges(candles)
    # Wilder's directional indicators smooth DM and TR over the same window,
    # starting at index 1 because bar 0 has no previous bar to compare against.
    smoothed_tr = wilder_smooth(tr[1:], period)
    smoothed_plus = wilder_smooth(plus_dm[1:], period)
    smoothed_minus = wilder_smooth(minus_dm[1:], period)

    plus_di: Series = [None] * size
    minus_di: Series = [None] * size
    dx_values: list[float] = []
    dx_index: list[int] = []

    for offset in range(len(smoothed_tr)):
        tr_value = smoothed_tr[offset]
        plus_value = smoothed_plus[offset]
        minus_value = smoothed_minus[offset]
        if tr_value is None or plus_value is None or minus_value is None or tr_value == 0:
            continue
        index = offset + 1
        pdi = 100.0 * plus_value / tr_value
        mdi = 100.0 * minus_value / tr_value
        plus_di[index] = pdi
        minus_di[index] = mdi
        denominator = pdi + mdi
        if denominator > 0:
            dx_values.append(100.0 * abs(pdi - mdi) / denominator)
            dx_index.append(index)

    adx_series: Series = [None] * size
    smoothed_dx = wilder_smooth(dx_values, period)
    for offset, value in enumerate(smoothed_dx):
        if value is not None:
            adx_series[dx_index[offset]] = value
    return adx_series, plus_di, minus_di


def bollinger(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[Series, Series, Series]:
    """Bollinger bands: upper, middle (SMA), lower."""
    middle = sma(values, period)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        mean = middle[i]
        if mean is None:
            continue
        window = values[i - period + 1 : i + 1]
        deviation = _population_stdev(window, mean)
        upper[i] = mean + num_std * deviation
        lower[i] = mean - num_std * deviation
    return upper, middle, lower


def _population_stdev(window: Sequence[float], mean: float) -> float:
    variance = sum((value - mean) ** 2 for value in window) / len(window)
    return math.sqrt(variance)


def donchian(candles: Sequence[Candle], lookback: int = 20) -> tuple[Series, Series]:
    """Donchian channel, computed over the bars *before* each bar.

    Excluding the current bar matters: a breakout test that includes the
    current bar's own high can never be exceeded by that bar, so the signal
    would never fire.
    """
    size = len(candles)
    upper: Series = [None] * size
    lower: Series = [None] * size
    for i in range(lookback, size):
        window = candles[i - lookback : i]
        upper[i] = max(float(c.high) for c in window)
        lower[i] = min(float(c.low) for c in window)
    return upper, lower


def log_returns(values: Sequence[float]) -> list[float]:
    """Per-bar log returns; length is ``len(values) - 1``."""
    out: list[float] = []
    for i in range(1, len(values)):
        previous, current = values[i - 1], values[i]
        if previous > 0 and current > 0:
            out.append(math.log(current / previous))
    return out


def realized_volatility(values: Sequence[float], period: int = 20) -> float | None:
    """Standard deviation of the last ``period`` log returns, as a ratio.

    Returned per-bar (not annualized): the sizing model divides a risk budget by
    this, so it only needs to be on the same time scale as the bars.
    """
    returns = log_returns(values)
    if len(returns) < period or period < 2:
        return None
    window = returns[-period:]
    mean = sum(window) / len(window)
    return math.sqrt(sum((r - mean) ** 2 for r in window) / (len(window) - 1))


def zscore(values: Sequence[float], period: int = 20) -> float | None:
    """How many standard deviations the latest value sits from its mean."""
    if len(values) < period or period < 2:
        return None
    window = values[-period:]
    mean = sum(window) / len(window)
    deviation = math.sqrt(sum((v - mean) ** 2 for v in window) / (len(window) - 1))
    if deviation == 0:
        return 0.0
    return (window[-1] - mean) / deviation


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """Clamp a raw statistic into a signal's bounded score range."""
    return max(low, min(high, value))
