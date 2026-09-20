from __future__ import annotations

import numpy as np

from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.indicators import obv


class VolumeSignal:
    """OBV trend vs. price trend (confirmation or divergence), plus a volume
    spike component - a non-price-derived check on the price-based signals."""

    name = "volume"

    def __init__(self, lookback: int = 20, spike_window: int = 20):
        self.lookback = lookback
        self.spike_window = spike_window

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        df = snapshot.candles
        min_len = max(self.lookback, self.spike_window) + 1
        if len(df) < min_len:
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient history"
            )

        obv_series = obv(df)
        recent_obv = obv_series.iloc[-self.lookback :]
        recent_close = df["close"].iloc[-self.lookback :]

        obv_slope = np.polyfit(range(len(recent_obv)), recent_obv.to_numpy(), 1)[0]
        price_slope = np.polyfit(range(len(recent_close)), recent_close.to_numpy(), 1)[0]

        obv_dir = float(np.sign(obv_slope))
        price_dir = float(np.sign(price_slope))

        avg_volume = df["volume"].iloc[-self.spike_window :].mean()
        latest_volume = df["volume"].iloc[-1]
        spike_ratio = (latest_volume / avg_volume) if avg_volume else 1.0

        if obv_dir != 0 and obv_dir == price_dir:
            value = price_dir * min(1.0, 0.4 + max(0.0, spike_ratio - 1) * 0.2)
            rationale = "volume confirms price trend"
        elif obv_dir != 0 and price_dir != 0 and obv_dir != price_dir:
            # OBV diverging from price is a classic early-warning signal -
            # it points the OTHER way from where price has been going.
            value = obv_dir * 0.5
            rationale = "volume/price divergence"
        else:
            value = 0.0
            rationale = "no clear volume signal"

        value = max(-1.0, min(1.0, value))
        confidence = max(0.0, min(1.0, max(0.0, spike_ratio - 1) * 0.5 + 0.3))
        return SignalScore(source=self.name, value=value, confidence=confidence, rationale=rationale)
