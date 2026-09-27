# Strategy: the trend ladder

The agent trades one rule on BTC-USD and ETH-USD. The code is in
`src/robinhood_crypto_agent/strategy/ladder.py`, and the settings are in
`config/strategy.yaml`. `rhca backtest` replays the same function the live
pipeline calls, so its `ladder (anchor) +50d exit` row is the evidence for
exactly what the agent proposes. A test
(`test_live_trades_exactly_what_the_backtest_trades`) feeds both paths the
same bars and requires the same trades.

It replaced System 1 on 2026-09-27: indicators, news labels, an escalation
trigger and Claude Sonnet as a second opinion. Why is below, under
[History](#history).

## The rule, on each closed hourly bar

**The anchor.** While the agent holds none of a coin, the anchor is its
highest close since the agent last sold out of it, or since `anchor_since`,
whichever is later. A dip is measured from it. Once a step is bought the
anchor stays fixed until the position is empty again. That span is one
*cycle*.

**Buys.** Each step is `percent:dollars`. With the default `5:5,10:10,20:20`:

| Close vs the anchor | Buys | Sells (anchor mode) |
|---|---|---|
| 5% under | $5 | — |
| 10% under | $10 | — |
| 20% under | $20 | — |
| 5% over | — | $5 worth |
| 10% over | — | $10 worth |
| 20% over | — | everything left |

Each step buys once and sells once per cycle, so at most $35 is in a coin at
a time.

**The trend filter.** A buy also needs the bar to close *above* the average
of the last 50 days of hourly closes (1,200 bars). At or under it, or before
the average exists, nothing is bought.

**The trend exit.** A close at or under that average sells everything the
ladder holds. The filter alone only stops buying in a downtrend, so a coin
bought just before the turn rode the whole decline down. The exit closes it
instead. It is also the one rule here added without a backtest: see
[Validating it](#validating-it).

## What it holds and remembers

The rule itself is stateless. What it needs is rebuilt from the audit log on
every analysis (`ledger.py`):

- **Holdings** are the ladder's own recorded fills, first in first out. A
  coin bought outside the agent, or by the retired System 1, is not the
  ladder's, and is never proposed for sale.
- **The cycle's anchor** is the one recorded on its first buy.
- **Steps taken** are those with an order that filled any amount, or that is
  still open. So a step is never proposed twice while its order is working.
  Record a canceled order's final state to free its step.

## How it turns into orders

Each thing the rule wants becomes one proposal: one limit order at the ask
for a buy, or at the bid for a sell, which is how the backtest fills.
Proposals go through the 14 risk rules and the approve-by-id gate like
anything else. A sell closes a position the ladder opened, so it is exempt
from the two caps on *new* exposure (`per_trade_notional` and
`daily_notional`), and says so. The kill switch and `sell_coverage` still
apply.

`rhca run` re-runs the rule every minute on the last closed bar, and logs
each proposal once per bar for as long as the rule still wants it. A fresh
proposal each bar keeps its price inside the approval gate's 0.5% drift
tolerance.

When there is nothing to do, `rhca analyze` says where the ladder stands:
the close against the anchor, what is held, the price at which the next step
buys or sells, and which side of the average the close is on.

## Validating it

`rhca backtest` compares the ladder against two baselines:

- **`trend +50d`**: $100 held while the close is above the average, nothing
  at or under it. It asks whether the ladder adds anything to the average
  alone.
- **`hold`**: $100 bought on the first bar and held.

The ladder runs in both sell modes, and as `+50d` (filter only) and
`+50d exit` (filter and exit). `--roll-window` re-runs everything over many
windows, each starting flat, so the result is not one lucky start date.

**The bar it has to clear.** This was set before any result with the exit
was seen:

1. It beats `hold` on capital in every window run: 180, 365 and 730 days,
   in both the normal and the stressed run.
2. It does so on BTC and ETH separately, not just pooled.
3. In the worst year, it loses no more than half of what `hold` lost.
4. In the rolling windows, it beats `hold` in most windows, not just the
   median one.

**Watch the trades column.** The exit compares each *hourly* close against
the average. When price hovers near it, the close can cross it many times.
Each crossing that sells and later rebuys costs the ~1.9% round trip. On a
synthetic random walk, the `+50d exit` ladders traded about six times as
often as without the exit, and `trend +50d` traded hundreds of times a year.
Real prices trend more than a random walk, but if the real `+50d exit` rows
trade far more than the `+50d` rows and lose to them, the exit is churning.
Set `trend_exit: false` in `config/strategy.yaml`, or ask for a band (exit
only a set percentage under the average), which is a new rule to test, not a
tweak.

A backtest that passes still authorizes nothing. It does not change a limit
or the approval gate, and it says nothing about the next trade.

## History

The backtests that led here, all at the 1.9% round trip:

- **System 1**: hourly indicators, six-hour holds. A trade has to move more
  than 1.9% in six hours to break even, which is about a typical six-hour
  BTC move. Its live record matched: 0 wins in 52 decided sells, and 0% on
  both sides for BTC and ETH.
- **The unfiltered ladder** lost about as much per dollar as holding over the
  year to 2026-09-27 (−43% vs −47%). It bought its steps in the first dip and
  held them as the market kept falling.
- **The 50-day filter** cut that year's loss to about −27%. On BTC and ETH
  alone, `ladder (anchor) +50d` beat `hold` per dollar in the 180, 365 and
  730-day windows. It still lost 18% on capital in the bad year, because
  nothing sold what it had bought. The trend exit is the fix to that.
- **SOL, SHIB and DOGE** lost under every ladder variant over two years, so
  the watchlist is BTC and ETH.
