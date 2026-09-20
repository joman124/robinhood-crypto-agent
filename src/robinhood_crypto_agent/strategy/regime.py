from __future__ import annotations

import pandas as pd

from robinhood_crypto_agent.models import Regime
from robinhood_crypto_agent.strategy.indicators import adx


def detect_regime(
    candles: pd.DataFrame, adx_period: int = 14, adx_trending_threshold: float = 25.0
) -> Regime:
    """Classify the current market regime from OHLCV candles.

    ADX above the threshold => TRENDING, otherwise RANGING. Cheap, well
    understood, and computable from OHLCV alone. Insufficient history (ADX
    is NaN) is treated as RANGING - the more conservative choice, since
    ranging weights favor mean-reversion + sentiment over trend-chasing.
    """
    adx_series = adx(candles, period=adx_period)
    latest = adx_series.iloc[-1] if len(adx_series) else float("nan")
    if pd.isna(latest):
        return Regime.RANGING
    return Regime.TRENDING if latest > adx_trending_threshold else Regime.RANGING
