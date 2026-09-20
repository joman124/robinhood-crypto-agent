from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from robinhood_crypto_agent.models import SignalScore


@dataclass
class MarketSnapshot:
    """Everything a SignalSource needs to score one symbol at a point in time.

    `candles` must be ascending by timestamp with open/high/low/close/volume
    columns. `watchlist_candles` (including `candles` itself, keyed by
    `symbol`) is used for cross-asset comparisons like relative strength.
    `fear_greed_index` is market-wide (0-100), not symbol-specific.
    """

    symbol: str
    candles: pd.DataFrame
    watchlist_candles: dict[str, pd.DataFrame] = field(default_factory=dict)
    fear_greed_index: float | None = None


class SignalSource(Protocol):
    name: str

    def score(self, snapshot: MarketSnapshot) -> SignalScore:
        """Return this source's opinion on `snapshot.symbol`.

        Must always return a SignalScore - use value=0.0, confidence=0.0 to
        mean "no opinion" (e.g. insufficient history) rather than raising.
        """
        ...
