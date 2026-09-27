"""Bootstrapping price history from Coinbase's public candles.

Robinhood's crypto API has no historical bars (docs/data-constraints.md), so a
fresh checkout would need 30 hours of polling before the indicators could say
anything. Coinbase publishes free candles for the same pairs; one request per
symbol returns up to 300 bars (12.5 days at 60 minutes).

Imported bars are marked ``source=import``, and only the indicators read them.
Proposals are always priced off a live Robinhood quote -- whose spread these
prices do not include -- and outcomes are scored on bars recorded afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import net
from .errors import AgentError
from .models import Candle, utcnow
from .numeric import to_decimal
from .symbols import canonical

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

#: The bar sizes Coinbase serves, in minutes.
COINBASE_GRANULARITIES = frozenset({1, 5, 15, 60, 360, 1440})


def fetch_coinbase_candles(
    symbol: str,
    *,
    interval_minutes: int,
    http: Callable[..., Any] = net.request_json,
    now: datetime | None = None,
) -> list[Candle]:
    """Closed bars for ``symbol``, oldest first. The still-forming bar is dropped."""
    if interval_minutes not in COINBASE_GRANULARITIES:
        raise AgentError(
            f"Coinbase has no {interval_minutes}-minute candles; it serves "
            f"{sorted(COINBASE_GRANULARITIES)} minutes"
        )
    product = canonical(symbol)
    url = (
        COINBASE_CANDLES_URL.format(product=product)
        + f"?granularity={interval_minutes * 60}"
    )
    rows = http("GET", url)
    return _parse_candles(rows, product, interval_minutes, cutoff=now or utcnow())


#: Coinbase returns at most this many candles per request.
MAX_CANDLES_PER_REQUEST = 300


def fetch_coinbase_history(
    symbol: str,
    *,
    interval_minutes: int,
    days: int,
    http: Callable[..., Any] = net.request_json,
    now: datetime | None = None,
) -> list[Candle]:
    """``days`` of closed bars, oldest first, paging back 300 bars at a time.

    For backtests: the live loop only needs the last 12.5 days that one
    request returns. Stops early when Coinbase has nothing older.
    """
    if interval_minutes not in COINBASE_GRANULARITIES:
        raise AgentError(
            f"Coinbase has no {interval_minutes}-minute candles; it serves "
            f"{sorted(COINBASE_GRANULARITIES)} minutes"
        )
    product = canonical(symbol)
    cutoff = now or utcnow()
    span = timedelta(minutes=interval_minutes)
    oldest = cutoff - timedelta(days=days)
    end = cutoff
    seen: dict[datetime, Candle] = {}
    while end > oldest:
        start = max(oldest, end - span * MAX_CANDLES_PER_REQUEST)
        url = (
            COINBASE_CANDLES_URL.format(product=product)
            + f"?granularity={interval_minutes * 60}"
            + f"&start={_iso(start)}&end={_iso(end)}"
        )
        page = _parse_candles(http("GET", url), product, interval_minutes, cutoff=cutoff)
        if not page:
            break
        for candle in page:
            seen[candle.start] = candle
        end = start
    return sorted(seen.values(), key=lambda c: c.start)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_candles(
    rows: Any, product: str, interval_minutes: int, *, cutoff: datetime
) -> list[Candle]:
    """Coinbase ``[time, low, high, open, close, volume]`` rows as closed bars."""
    if not isinstance(rows, list):
        raise AgentError(f"unexpected Coinbase candles response for {product}: {str(rows)[:200]}")
    span = timedelta(minutes=interval_minutes)
    candles: list[Candle] = []
    for row in rows:
        # Each row is [time, low, high, open, close, volume].
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            start = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
            low, high, open_price, close = (
                to_decimal(value, field=name)
                for value, name in zip(row[1:5], ("low", "high", "open", "close"), strict=True)
            )
        except (TypeError, ValueError, OverflowError, AgentError):
            continue
        if start + span > cutoff:
            continue
        candles.append(
            Candle(
                symbol=product,
                start=start,
                end=start + span,
                open=open_price,
                high=high,
                low=low,
                close=close,
                observations=4,
            )
        )
    candles.sort(key=lambda c: c.start)
    return candles
