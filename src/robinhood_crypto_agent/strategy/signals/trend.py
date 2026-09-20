from __future__ import annotations

import pandas as pd

from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.indicators import macd, sma


class TrendSignal:
    """SMA-confirmed MACD histogram: trend direction and strength."""

    name = "trend"

    def __init__(
        self,
        fast_span: int = 12,
        slow_span: int = 26,
        signal_span: int = 9,
        trend_window: int = 50,
    ):
        self.fast_span = fast_span
        self.slow_span = slow_span
        self.signal_span = signal_span
        self.trend_window = trend_window

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        close = snapshot.candles["close"]
        if len(close) < max(self.slow_span + self.signal_span, self.trend_window):
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient history"
            )

        _, _, histogram = macd(close, self.fast_span, self.slow_span, self.signal_span)
        trend_sma = sma(close, self.trend_window)

        latest_hist = histogram.iloc[-1]
        latest_close = close.iloc[-1]
        latest_trend = trend_sma.iloc[-1]

        if pd.isna(latest_hist) or pd.isna(latest_trend):
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient history"
            )

        # Normalize the histogram by price so magnitude is comparable across
        # symbols with very different price scales.
        norm_hist = (latest_hist / latest_close) if latest_close else 0.0
        value = max(-1.0, min(1.0, norm_hist * 50))

        above_trend = latest_close > latest_trend
        if above_trend and value < 0:
            value *= 0.5  # trend confirmation dampens a contrary MACD reading
        elif not above_trend and value > 0:
            value *= 0.5

        confidence = min(1.0, abs(norm_hist) * 100)
        rationale = (
            f"MACD histogram {latest_hist:.4g}, price "
            f"{'above' if above_trend else 'below'} SMA{self.trend_window}"
        )
        return SignalScore(source=self.name, value=value, confidence=confidence, rationale=rationale)
