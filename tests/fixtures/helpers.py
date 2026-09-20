from __future__ import annotations

import numpy as np
import pandas as pd


def make_trending_ohlcv(
    n: int = 100,
    start_price: float = 100.0,
    daily_return: float = 0.01,
    volume: float = 1_000.0,
    noise: float = 0.0005,
    seed: int = 0,
) -> pd.DataFrame:
    """A steadily trending (up or down, depending on sign of daily_return)
    synthetic OHLCV series - a deterministic ADX-trending fixture."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")

    prices = [start_price]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + daily_return + rng.normal(0, noise)))
    close = pd.Series(prices, index=dates)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([open_, close], axis=1).max(axis=1) * 1.001
    low = pd.concat([open_, close], axis=1).min(axis=1) * 0.999
    vol = pd.Series(volume, index=dates)

    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol})


def make_flat_ohlcv(n: int = 100, price: float = 100.0, volume: float = 1_000.0) -> pd.DataFrame:
    """A perfectly flat series - an ADX-ranging fixture."""
    dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    close = pd.Series(price, index=dates)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.0001,
            "low": close * 0.9999,
            "close": close,
            "volume": pd.Series(volume, index=dates),
        }
    )


def make_linear_trend_ohlcv(
    n: int = 100, start_price: float = 100.0, daily_delta: float = 1.0, volume: float = 1_000.0
) -> pd.DataFrame:
    """A series with constant ABSOLUTE daily price change (not a compounding
    percentage). Unlike make_trending_ohlcv, this never decelerates in
    dollar terms as price moves, so MACD histogram sign stays unambiguous -
    useful for testing trend-following signal direction without the
    deceleration artifacts a compounding % series produces."""
    dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    close = pd.Series([start_price + i * daily_delta for i in range(n)], index=dates)
    open_ = close.shift(1).fillna(close.iloc[0])
    pad = abs(daily_delta) * 0.1 + 0.01
    high = pd.concat([open_, close], axis=1).max(axis=1) + pad
    low = pd.concat([open_, close], axis=1).min(axis=1) - pad
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": pd.Series(volume, index=dates)}
    )


def make_oscillating_ohlcv(
    n: int = 100, price: float = 100.0, amplitude: float = 5.0, period: int = 10, volume: float = 1_000.0
) -> pd.DataFrame:
    """A sine-wave oscillation around a fixed price - reaches genuine RSI
    extremes without trending, useful for mean-reversion tests."""
    dates = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    t = np.arange(n)
    close = pd.Series(price + amplitude * np.sin(2 * np.pi * t / period), index=dates)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([open_, close], axis=1).max(axis=1) + 0.1
    low = pd.concat([open_, close], axis=1).min(axis=1) - 0.1
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": pd.Series(volume, index=dates)}
    )
