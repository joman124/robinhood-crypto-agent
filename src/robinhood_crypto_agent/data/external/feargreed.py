from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
from diskcache import Cache

FEAR_GREED_API_BASE_DEFAULT = "https://api.alternative.me/fng"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "cache"


class FearGreedClient:
    """Free, no-API-key crypto Fear & Greed Index client
    (https://alternative.me/crypto/fear-and-greed-index/). Market-wide, not
    symbol-specific - see strategy/signals/sentiment.py."""

    def __init__(
        self,
        api_base: str = FEAR_GREED_API_BASE_DEFAULT,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        timeout_s: float = 15.0,
        cache_ttl_s: int = 60 * 30,
    ):
        self.api_base = api_base
        self.cache = Cache(str(cache_dir))
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s

    def current(self) -> float | None:
        """Latest Fear & Greed Index value (0-100), or None on any failure.

        Callers must treat `None` as "no opinion" - never fabricate a value
        (see CLAUDE.md).
        """
        cache_key = "fng:current"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            response = httpx.get(self.api_base, params={"limit": 1}, timeout=self.timeout_s)
            response.raise_for_status()
            value = float(response.json()["data"][0]["value"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError):
            return None

        self.cache.set(cache_key, value, expire=self.cache_ttl_s)
        return value

    def historical(self, limit: int = 0) -> pd.DataFrame:
        """Historical daily Fear & Greed values, indexed by UTC date, single
        `value` column. `limit=0` requests the API's full available
        history."""
        cache_key = f"fng:historical:{limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        response = httpx.get(
            self.api_base, params={"limit": limit, "format": "json"}, timeout=self.timeout_s
        )
        response.raise_for_status()
        payload = response.json()

        records = [
            {
                "date": datetime.fromtimestamp(int(item["timestamp"]), tz=UTC).date(),
                "value": float(item["value"]),
            }
            for item in payload["data"]
        ]
        df = pd.DataFrame(records).set_index("date").sort_index()
        self.cache.set(cache_key, df, expire=self.cache_ttl_s)
        return df
