"""An append-only store of observed quotes, and the bars derived from them.

**Why this module exists.** The RobinHood MCP server exposes
``get_equity_historicals``, ``get_index_historicals`` and
``get_option_historicals`` -- but no crypto equivalent. ``get_crypto_quotes``
returns only the current bid/ask/mark plus the previous close. So there is no
way to ask the server "what did BTC do over the last 30 hours?".

Any indicator-based crypto strategy therefore has to build its own history.
This store is that history: each ``get_crypto_quotes`` response Claude ingests
is appended as a line of JSONL, and bars are aggregated from those observations
on read. The consequences are deliberate and visible rather than hidden:

* A fresh checkout has **no** history and can evaluate nothing. That is the
  honest state, and :meth:`PriceStore.coverage` reports it so the CLI can say
  "3 of 30 bars" instead of emitting a confident signal from three ticks.
* Bar quality depends on sampling rate. :attr:`Candle.observations` carries the
  sample count so a strategy can discount a bar built from a single tick.
* History can be bootstrapped from an external OHLC source via
  :meth:`PriceStore.import_candles` for backtesting, which keeps the "where did
  this number come from" question answerable -- imported bars are marked.

The file format is JSONL: append-only, survives a crash mid-write with at most
one corrupt trailing line, and is greppable. Corrupt lines are skipped on read
rather than aborting the load, because losing one observation must not make the
whole history unreadable.

Reads are incremental. Because the file only ever grows, a store remembers how
far it has read and parses only what was appended since -- which is what lets
``rhca run`` re-evaluate every minute without re-reading weeks of quotes. A
file that shrank (replaced or truncated) is simply read again from the start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..errors import AgentError
from ..models import Candle, Quote, parse_timestamp, utcnow
from ..numeric import ZERO, format_decimal, to_decimal
from ..symbols import canonical

#: Marker written on rows that came from an external source rather than a quote.
SOURCE_QUOTE = "quote"
SOURCE_IMPORT = "import"


@dataclass(frozen=True)
class Coverage:
    """How much usable history exists for one symbol."""

    symbol: str
    observations: int
    bars: int
    first_seen: datetime | None
    last_seen: datetime | None
    required_bars: int

    @property
    def sufficient(self) -> bool:
        return self.bars >= self.required_bars

    @property
    def age_seconds(self) -> float | None:
        if self.last_seen is None:
            return None
        return (utcnow() - self.last_seen).total_seconds()

    def describe(self) -> str:
        if self.observations == 0:
            return f"{self.symbol}: no observations recorded"
        status = "ok" if self.sufficient else "INSUFFICIENT"
        return (
            f"{self.symbol}: {self.bars}/{self.required_bars} bars "
            f"from {self.observations} observations [{status}]"
        )


def floor_to_interval(moment: datetime, interval_minutes: int) -> datetime:
    """Floor a timestamp to the start of its bar.

    Bars are anchored to the UTC epoch day rather than to the first observation,
    so the same wall-clock minute always lands in the same bucket no matter when
    the store was started -- otherwise re-running an analysis after a restart
    could shift every bar boundary and change the signals.
    """
    if interval_minutes < 1:
        raise AgentError("interval_minutes must be at least 1")
    moment = moment.astimezone(timezone.utc)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((moment - day_start).total_seconds() // 60)
    return day_start + timedelta(minutes=(elapsed // interval_minutes) * interval_minutes)


class PriceStore:
    """Append-only JSONL store of price observations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._rows: list[dict[str, object]] = []
        self._by_symbol: dict[str, list[Quote]] = {}
        self._offset = 0

    # -- writing ---------------------------------------------------------

    def record_quotes(self, quotes: Iterable[Quote]) -> int:
        """Append quote observations. Returns how many rows were written."""
        rows = [
            {
                "symbol": quote.symbol,
                "observed_at": quote.observed_at.astimezone(timezone.utc).isoformat(),
                "bid": format_decimal(quote.bid),
                "ask": format_decimal(quote.ask),
                "mark": format_decimal(quote.mark),
                "previous_close": (
                    format_decimal(quote.previous_close)
                    if quote.previous_close is not None
                    else None
                ),
                "source": SOURCE_QUOTE,
            }
            for quote in quotes
        ]
        return self._append(rows)

    def import_candles(self, candles: Iterable[Candle]) -> int:
        """Import externally-sourced OHLC bars as synthetic observations.

        Each bar becomes four observations -- open, high, low, close, timestamped
        across the bar -- so that re-aggregation at the same or a coarser
        interval reproduces the bar's extremes. Rows are marked
        ``source: "import"`` so imported history is always distinguishable from
        what the agent actually observed.
        """
        rows: list[dict[str, object]] = []
        for candle in candles:
            span = (candle.end - candle.start) or timedelta(minutes=1)
            # The close is timestamped just *inside* the bar. Using candle.end
            # would place it at the next bar's start, where re-aggregation would
            # read it as that bar's open -- shifting every close forward by one
            # bar and inventing a trailing bar out of the last close.
            close_at = max(candle.start, candle.end - timedelta(microseconds=1))
            points = [
                (candle.start, candle.open),
                (candle.start + span / 3, candle.high),
                (candle.start + span * 2 / 3, candle.low),
                (close_at, candle.close),
            ]
            for moment, price in points:
                rows.append(
                    {
                        "symbol": canonical(candle.symbol),
                        "observed_at": moment.astimezone(timezone.utc).isoformat(),
                        "bid": format_decimal(price),
                        "ask": format_decimal(price),
                        "mark": format_decimal(price),
                        "previous_close": None,
                        "source": SOURCE_IMPORT,
                    }
                )
        return self._append(rows)

    def _append(self, rows: Sequence[dict[str, object]]) -> int:
        if not rows:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        return len(rows)

    # -- reading ---------------------------------------------------------

    def _refresh(self) -> None:
        """Parse whatever was appended since the last read."""
        if not self.path.exists():
            self._rows, self._by_symbol, self._offset = [], {}, 0
            return
        size = self.path.stat().st_size
        if size < self._offset:
            self._rows, self._by_symbol, self._offset = [], {}, 0
        if size == self._offset:
            return
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(size - self._offset)
        # Only complete lines are consumed; a line still being written is left
        # for the next read rather than parsed half-finished.
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._offset += end + 1

        touched: set[str] = set()
        for line in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A torn line from an interrupted write. Skipping one
                # observation is strictly better than refusing to read the
                # history at all.
                continue
            if not isinstance(row, dict):
                continue
            self._rows.append(row)
            quote = _quote_from_row(row)
            if quote is not None:
                self._by_symbol.setdefault(quote.symbol, []).append(quote)
                touched.add(quote.symbol)
        # Imports can land out of order, so re-sort only what changed.
        for symbol in touched:
            self._by_symbol[symbol].sort(key=lambda q: q.observed_at)

    def _iter_rows(self) -> Iterator[dict[str, object]]:
        self._refresh()
        return iter(self._rows)

    def observations(
        self, symbol: str, *, since: datetime | None = None
    ) -> list[Quote]:
        """All recorded observations for ``symbol``, oldest first."""
        self._refresh()
        quotes = self._by_symbol.get(canonical(symbol), [])
        if since is None:
            return list(quotes)
        return [q for q in quotes if q.observed_at >= since]

    def symbols(self) -> list[str]:
        """Every symbol with at least one observation."""
        seen = {canonical(str(row.get("symbol", ""))) for row in self._iter_rows()}
        seen.discard("")
        return sorted(seen)

    def candles(
        self,
        symbol: str,
        *,
        interval_minutes: int = 60,
        limit: int | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        """Aggregate observations into OHLC bars, oldest first.

        The most recent bar is usually still forming. It is excluded unless
        ``include_partial`` is set, because an indicator computed on a bar that
        is 5 minutes into its hour will change under its own feet -- and a
        signal that flips between two runs minutes apart is worse than no
        signal.
        """
        quotes = self.observations(symbol)
        if not quotes:
            return []

        buckets: dict[datetime, list[Quote]] = {}
        for quote in quotes:
            buckets.setdefault(floor_to_interval(quote.observed_at, interval_minutes), []).append(quote)

        candles: list[Candle] = []
        for start in sorted(buckets):
            window = buckets[start]
            marks = [q.mark for q in window]
            candles.append(
                Candle(
                    symbol=canonical(symbol),
                    start=start,
                    end=start + timedelta(minutes=interval_minutes),
                    open=window[0].mark,
                    high=max(marks),
                    low=min(marks),
                    close=window[-1].mark,
                    observations=len(window),
                )
            )

        if candles and not include_partial:
            current_bar = floor_to_interval(utcnow(), interval_minutes)
            if candles[-1].start >= current_bar:
                candles.pop()

        if limit is not None and limit > 0:
            candles = candles[-limit:]
        return candles

    def coverage(
        self, symbol: str, *, interval_minutes: int = 60, required_bars: int = 30
    ) -> Coverage:
        """Report how much history exists, without pretending there is more."""
        quotes = self.observations(symbol)
        bars = self.candles(symbol, interval_minutes=interval_minutes)
        return Coverage(
            symbol=canonical(symbol),
            observations=len(quotes),
            bars=len(bars),
            first_seen=quotes[0].observed_at if quotes else None,
            last_seen=quotes[-1].observed_at if quotes else None,
            required_bars=required_bars,
        )

    def latest_quote(self, symbol: str) -> Quote | None:
        """The most recently observed quote for ``symbol``."""
        quotes = self.observations(symbol)
        return quotes[-1] if quotes else None


def _quote_from_row(row: dict[str, object]) -> Quote | None:
    """A stored row as a Quote, or ``None`` when it has no usable mark."""
    symbol = canonical(str(row.get("symbol", "")))
    if not symbol:
        return None
    try:
        observed_at = parse_timestamp(str(row["observed_at"]))
        mark = to_decimal(row["mark"], field="mark")
    except (KeyError, ValueError, AgentError):
        return None
    if mark <= ZERO:
        return None
    return Quote(
        symbol=symbol,
        bid=_safe_decimal(row.get("bid")) or mark,
        ask=_safe_decimal(row.get("ask")) or mark,
        mark=mark,
        observed_at=observed_at,
        previous_close=_safe_decimal(row.get("previous_close")),
    )


def _safe_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = to_decimal(value)
    except AgentError:
        return None
    return parsed if parsed > ZERO else None
