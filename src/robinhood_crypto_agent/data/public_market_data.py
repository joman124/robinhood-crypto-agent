from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
from diskcache import Cache

COINGECKO_API_BASE_DEFAULT = "https://api.coingecko.com/api/v3"

# CoinGecko coin ids for the default watchlist (config/watchlist.yaml).
# Extend this when adding a symbol to the watchlist that isn't listed here.
SYMBOL_TO_COINGECKO_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "ADA": "cardano",
    "SHIB": "shiba-inu",
}

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache"


class UnknownSymbolError(ValueError):
    pass


class PublicMarketDataClient:
    """Free, no-API-key historical OHLCV for offline strategy iteration and
    backtesting - NOT used for anything that generates a live proposal meant
    for same-day execution (that always comes from the MCP server via
    `analyze --data-source mcp-json`; see docs/strategy.md)."""

    def __init__(
        self,
        api_base: str = COINGECKO_API_BASE_DEFAULT,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        timeout_s: float = 30.0,
        cache_ttl_s: int = 60 * 60 * 24,
    ):
        self.api_base = api_base
        self.cache = Cache(str(cache_dir))
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s

    def fetch_daily_ohlcv(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Daily OHLCV DataFrame indexed by UTC date, columns
        open/high/low/close/volume/symbol.

        Approximated by resampling CoinGecko's `market_chart/range`
        price+volume series into daily bars - the free tier doesn't offer
        true OHLC with volume together over arbitrary date ranges. Good
        enough for strategy validation; not a substitute for exchange-grade
        OHLCV.
        """
        coingecko_id = SYMBOL_TO_COINGECKO_ID.get(symbol.upper())
        if coingecko_id is None:
            raise UnknownSymbolError(
                f"No CoinGecko id mapping for symbol {symbol!r}; add one to "
                "SYMBOL_TO_COINGECKO_ID in data/public_market_data.py."
            )

        cache_key = f"ohlcv:{coingecko_id}:{start.date()}:{end.date()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        params: dict[str, str | int] = {
            "vs_currency": "usd",
            "from": int(start.replace(tzinfo=UTC).timestamp()),
            "to": int(end.replace(tzinfo=UTC).timestamp()),
        }
        url = f"{self.api_base}/coins/{coingecko_id}/market_chart/range"
        response = httpx.get(url, params=params, timeout=self.timeout_s)
        response.raise_for_status()
        payload = response.json()

        prices = pd.DataFrame(payload["prices"], columns=["ts_ms", "price"])
        volumes = pd.DataFrame(payload["total_volumes"], columns=["ts_ms", "volume"])
        merged = prices.merge(volumes, on="ts_ms", how="outer").sort_values("ts_ms")
        merged["timestamp"] = pd.to_datetime(merged["ts_ms"], unit="ms", utc=True)
        merged = merged.set_index("timestamp")

        daily = merged["price"].resample("1D").ohlc()
        daily["volume"] = merged["volume"].resample("1D").sum()
        daily = daily.dropna(subset=["open", "high", "low", "close"])
        daily["symbol"] = symbol.upper()
        daily.index = daily.index.date

        self.cache.set(cache_key, daily, expire=self.cache_ttl_s)
        return daily
