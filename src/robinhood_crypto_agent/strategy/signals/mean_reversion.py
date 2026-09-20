from __future__ import annotations

import pandas as pd

from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.indicators import rsi


class MeanReversionSignal:
    """RSI extremes: oversold => bullish tilt, overbought => bearish tilt."""

    name = "mean_reversion"

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        close = snapshot.candles["close"]
        if len(close) < self.period + 1:
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient history"
            )

        latest = rsi(close, self.period).iloc[-1]
        if pd.isna(latest):
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient history"
            )

        if latest <= self.oversold:
            depth = (self.oversold - latest) / self.oversold
            value = min(1.0, depth + 0.3)
            confidence = min(1.0, depth + 0.4)
            rationale = f"RSI {latest:.1f} oversold (<= {self.oversold})"
        elif latest >= self.overbought:
            span = 100 - self.overbought
            depth = (latest - self.overbought) / span
            value = -min(1.0, depth + 0.3)
            confidence = min(1.0, depth + 0.4)
            rationale = f"RSI {latest:.1f} overbought (>= {self.overbought})"
        else:
            value = 0.0
            confidence = 0.1
            rationale = f"RSI {latest:.1f} neutral"

        return SignalScore(source=self.name, value=value, confidence=confidence, rationale=rationale)
