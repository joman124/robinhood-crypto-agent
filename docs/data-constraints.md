# The data constraint that shapes this agent

## The problem

The RobinHood MCP server exposes historical price tools for equities, indexes
and options:

- `get_equity_historicals`
- `get_index_historicals`
- `get_option_historicals`

There is **no crypto equivalent**. The only crypto price tool is
`get_crypto_quotes`, which returns a snapshot:

```json
{"symbol": "BTCUSD", "bid_price": "80466.2691145", "ask_price": "81982.17105482",
 "mark_price": "81224.22008466", "open_price": "80469.17",
 "updated_at": "2026-09-20T13:32:55.452-04:00"}
```

`open_price` is the previous close — one prior data point, not a series. So
there is no way to ask the server "what did BTC do over the last 50 days?",
and the trend ladder needs exactly that.

This is not a gap that can be papered over. The ladder's trend average needs
1,200 hourly closes (50 days), and its anchor is the highest close since the
last cycle. Without history, it has nothing to compute.

## What this agent does instead

**It records what it is given and builds its own history.**

Every `get_crypto_quotes` response ingested through `rhca ingest quotes` is
appended to `data/price_history.jsonl` as an observation. Bars are aggregated
from those observations on read, bucketed by interval
(`strategy.bar_interval_minutes`, default 60).

```
observations:  09:03  09:19  09:34  09:51 | 10:02  10:18  ...
bar:           └────────  09:00  ───────┘ └──── 10:00 ────
               open=first  high=max  low=min  close=last  observations=4
```

Three consequences, all made visible rather than hidden:

### 1. A fresh checkout can evaluate nothing

That is the honest state, and the agent says so:

```
$ rhca status
price history coverage:
  BTC-USD: no observations recorded
  ETH-USD: no observations recorded
```

`rhca analyze` skips such a symbol with a reason rather than acting on
nothing:

> `BTC-USD (0 bars): no price history. The MCP server has no crypto
> historicals tool: rhca bootstrap-history imports Coinbase bars, and rhca run
> records quotes as it goes.`

With some bars but fewer than the average needs, it buys nothing and says so:
`its 50-day average needs 1200 bars and 300 are on hand`.

### 2. Gaps in polling are filled from Coinbase

A bar exists only for an hour in which a quote was recorded. The rule decides
on each closed bar's close, and the trend average counts bars, so an hour the
loop was not running would shorten the average's real span. `rhca run` fills
such gaps from Coinbase's hourly candles each time it starts.

### 3. History has to be bootstrapped or accumulated

Two options, and they compose:

**Accumulate.** Ingest quotes on a schedule. At 60-minute bars, the 1,200 bars
the trend average wants would take 50 days to accrue, so in practice:

**Bootstrap.** `rhca bootstrap-history` (and `rhca run` at startup) imports
52 days of Coinbase's hourly candles. Or import OHLC bars from any external
source you trust:

```bash
rhca import-history BTC-USD -f bars.json --interval 60
```

Accepts `start`/`open`/`high`/`low`/`close` or Robinhood's
`begins_at`/`open_price`/`high_price`/`low_price`/`close_price` spelling. Each
bar is written as four synthetic observations — open, high, low, close, spaced
across the bar and timestamped strictly *inside* it — so re-aggregation
reproduces the bar exactly. Imported rows are marked `source: "import"`, so
what the agent observed is always distinguishable from what it was handed.

## Why the store is a JSONL file

- **Append-only.** An interrupted write costs at most the last observation;
  torn trailing lines are skipped on read rather than failing the whole load.
- **Greppable.** "What price did we see at 14:00 on Tuesday?" is a `grep`.
- **Bars are derived, not stored.** Changing `bar_interval_minutes` re-buckets
  the same observations. Nothing needs migrating.

Bars are anchored to the UTC day boundary, not to the first observation, so the
same wall-clock minute always falls in the same bucket — restarting the store
does not shift every bar boundary and silently change a decision.

## The other quote quirks this forces you to handle

- **Symbol spelling differs by tool.** `get_crypto_quotes` returns `BTCUSD`;
  `get_currency_pairs` returns `BTC-USD`. Everything is normalized to the
  hyphenated form in `symbols.py`; without that, a watchlist entry silently
  fails to match its own quote.
- **Zero is not a price.** A zero `bid_price` or `ask_price` means that side of
  the book is unavailable, and a zero `mark_price` means the mark is unusable.
  Such values are rejected, never parsed into a number that could become a
  limit price.
- **The spread is wide.** The live BTC quote above shows a **1.87%** spread
  under market-maker routing. That is why proposals are priced against the side
  they actually cross — a buy against the ask, a sell against the bid — and why
  `max_spread_pct` is a real gate rather than a formality.
