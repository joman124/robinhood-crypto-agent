from __future__ import annotations

from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot


class SentimentSignal:
    """Contrarian signal from the crypto Fear & Greed Index (0=extreme fear,
    100=extreme greed): extreme fear tilts bullish, extreme greed tilts
    bearish. Market-wide, not symbol-specific - the one external,
    non-price-derived input in the default signal set."""

    name = "sentiment"

    def __init__(self, fear_threshold: float = 25.0, greed_threshold: float = 75.0):
        self.fear_threshold = fear_threshold
        self.greed_threshold = greed_threshold

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        index = snapshot.fear_greed_index
        if index is None:
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="no sentiment data"
            )

        if index <= self.fear_threshold:
            value = (self.fear_threshold - index) / self.fear_threshold
            rationale = f"Fear & Greed Index {index:.0f} - extreme fear (contrarian bullish)"
        elif index >= self.greed_threshold:
            span = 100 - self.greed_threshold
            value = -((index - self.greed_threshold) / span)
            rationale = f"Fear & Greed Index {index:.0f} - extreme greed (contrarian bearish)"
        else:
            value = 0.0
            rationale = f"Fear & Greed Index {index:.0f} - neutral"

        value = max(-1.0, min(1.0, value))
        confidence = min(1.0, abs(value) + 0.2) if value != 0 else 0.1
        return SignalScore(source=self.name, value=value, confidence=confidence, rationale=rationale)
