"""Closed UTC daily bars: what the split decides on, live and on paper.

The split's rules read daily closes -- a 20-day high, 100- and 200-day
averages, an ATR -- the same Coinbase daily candles ``rhca backtest`` and the
forward test read, so live, paper and backtest all decide on the same bars.
One request covers 300 days, so a coin costs one or two requests a day.

Bars are cached per coin under ``data/daily/``. A cache that already holds
the latest close is used as is; one that lacks it is refetched, but not more
often than every few minutes while Coinbase has not published the close yet,
since ``rhca run`` asks every minute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .bootstrap import fetch_coinbase_history
from .errors import AgentError
from .files import read_json, write_json_atomic
from .models import Candle, parse_timestamp, utcnow
from .numeric import format_decimal, to_decimal
from .symbols import canonical

DAY = timedelta(days=1)
DAILY_MINUTES = 24 * 60
#: How long a cache that lacks the latest close is trusted before refetching.
RETRY_AFTER = timedelta(minutes=10)

Fetch = Callable[..., list[Candle]]


def latest_close(now: datetime) -> datetime:
    """The start of the last UTC day that has closed."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - DAY


class DailyBars:
    """Coinbase's closed daily bars per coin, cached on disk and in memory."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetch: Fetch | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self._fetch = fetch
        self._now = now
        self._memory: dict[str, tuple[datetime, int, list[Candle]]] = {}

    def get(self, symbol: str, *, days: int) -> list[Candle]:
        """At least ``days`` closed daily bars, oldest first, through the
        latest close if Coinbase has published it."""
        symbol = canonical(symbol)
        now = self._now()
        through = latest_close(now)
        cached = self._memory.get(symbol) or self._load(symbol)
        if cached is not None:
            fetched_at, have_days, bars = cached
            complete = bool(bars) and bars[-1].start >= through
            if have_days >= days and (complete or now - fetched_at < RETRY_AFTER):
                self._memory[symbol] = cached
                return bars
        fetch = self._fetch or fetch_coinbase_history
        bars = fetch(symbol, interval_minutes=DAILY_MINUTES, days=days)
        if not bars:
            raise AgentError(f"Coinbase returned no daily bars for {symbol}")
        self._memory[symbol] = (now, days, bars)
        self._save(symbol, now, days, bars)
        return bars

    def _path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.json"

    def _load(self, symbol: str) -> tuple[datetime, int, list[Candle]] | None:
        data = read_json(self._path(symbol))
        if data is None:
            return None
        try:
            fetched_at = parse_timestamp(str(data["fetched_at"]))
            bars = [
                Candle(
                    symbol,
                    start := parse_timestamp(str(row["start"])),
                    start + DAY,
                    to_decimal(row["open"], field="open"),
                    to_decimal(row["high"], field="high"),
                    to_decimal(row["low"], field="low"),
                    to_decimal(row["close"], field="close"),
                    4,
                )
                for row in data.get("bars") or []
            ]
            return fetched_at, int(data.get("days", 0)), bars
        except (KeyError, TypeError, ValueError, AgentError):
            return None

    def _save(self, symbol: str, fetched_at: datetime, days: int, bars: list[Candle]) -> None:
        write_json_atomic(
            self._path(symbol),
            {
                "fetched_at": fetched_at.isoformat(),
                "days": days,
                "bars": [
                    {
                        "start": c.start.isoformat(),
                        "open": format_decimal(c.open),
                        "high": format_decimal(c.high),
                        "low": format_decimal(c.low),
                        "close": format_decimal(c.close),
                    }
                    for c in bars
                ],
            },
        )
