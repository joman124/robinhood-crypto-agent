from __future__ import annotations

import pandas as pd

from robinhood_crypto_agent.models import SignalScore
from robinhood_crypto_agent.strategy.base import MarketSnapshot
from robinhood_crypto_agent.strategy.indicators import momentum


class RelativeStrengthSignal:
    """Ranks this symbol's momentum against the rest of the watchlist - a
    cross-asset comparison rather than a single-symbol price-only signal."""

    name = "relative_strength"

    def __init__(self, window: int = 20):
        self.window = window

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        peers = snapshot.watchlist_candles
        if not peers or snapshot.symbol not in peers:
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="no peer data"
            )

        momenta: dict[str, float] = {}
        for symbol, candles in peers.items():
            close = candles["close"]
            if len(close) <= self.window:
                continue
            mom = momentum(close, self.window).iloc[-1]
            if pd.notna(mom):
                momenta[symbol] = float(mom)

        if snapshot.symbol not in momenta or len(momenta) < 2:
            return SignalScore(
                source=self.name, value=0.0, confidence=0.0, rationale="insufficient peer history"
            )

        this_mom = momenta[snapshot.symbol]
        others = [m for s, m in momenta.items() if s != snapshot.symbol]
        avg_other = sum(others) / len(others)

        rank = sum(1 for m in others if this_mom > m)
        value = (rank / len(others)) * 2 - 1  # map rank-among-peers to [-1, 1]

        spread = this_mom - avg_other
        confidence = max(0.0, min(1.0, abs(spread) * 10))

        rationale = (
            f"{self.window}-period momentum {this_mom:+.2%} vs peer avg {avg_other:+.2%}"
        )
        return SignalScore(source=self.name, value=value, confidence=confidence, rationale=rationale)
