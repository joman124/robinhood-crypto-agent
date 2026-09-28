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

**Status, 2026-09-27: it failed its own bar** on real BTC and ETH bars (see
[The trend exit, tested](#the-trend-exit-tested)), and no proposal of it
should be approved. Its successor candidate is the
[breakout](#the-breakout-candidate), which is backtest-only until it passes.

**Status, 2026-09-28:** the breakout scored 4 of 5 again over four years
([Over four years](#over-four-years)). The owner's plan -- $250 held
long-term, $250 in short-term trades -- is now a backtest
([The split](#the-split-long-term--short-term)) and a paper account that
`rhca run` keeps on live prices ([The forward test](#the-forward-test)).
Neither trades.

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
- **The trend exit** was tested on real bars the same day. See the next
  section.

## The trend exit, tested

On 2026-09-27, on BTC and ETH together (return on capital, 1.9% round trip):

| | 180d | 365d | 730d | 90-day windows beating hold | median window | worst window | trades, 2y |
|---|---|---|---|---|---|---|---|
| anchor +50d exit (the live rule) | +1.6% | −7.9% | −5.5% | 11/22 | −3.0% | −7.8% | 109 |
| anchor +50d | +40.2% | −17.9% | +27.0% | 11/22 | −1.3% | −33.8% | 29 |
| anchor | +29.9% | −27.4% | +22.4% | 15/22 | −0.6% | −25.0% | 31 |
| hold | +24.4% | −29.2% | +11.0% | — | −6.0% | −34.6% | 2 |

The live rule failed criteria 1 and 4: it lost to hold at 180 and 730 days
on both coins, and beat it in only 11 of 22 windows. The exit did what it was
for -- the worst window went from −34% to −8% -- but on hourly closes it sold
and rebought whenever the price wobbled across the average, and gave away
the upside in costs.

## The breakout candidate

### The best rule so far, and what is wrong with it

By total P&L, the best so far is `ladder (anchor) +50d`: +40% on capital over
180 days, +27% over 730, on BTC and ETH. Why it still is not worth trading:

1. **Its payoff is lopsided the wrong way.** It sells into strength at +5%,
   +10% and +20% over the old high, and it never sells a loser. Winners are
   capped; losers are held until they come back. At the end of the 730-day
   run it held $14 of unsold losses on $70 of capital, and a 90-day window
   lost 34%. A +150% run like XRP's is sold at +20%.
2. **It buys weakness.** Every entry is a dip. When a trend turns, the first
   dips are the start of the fall, and a lagging 50-day average still reads
   "up" -- which is how it bought early in the 2025-26 decline, and held
   those buys all the way down.
3. **Its steps ignore volatility.** A 5% dip is a big move for BTC and an
   ordinary day for SOL. The same step means different odds on every coin.
4. **Its size is tiny, and upside down.** Its biggest step, $20 at −20%, is
   almost never reachable while the price must stay above the average. In
   practice it holds $5 to $15 of a coin: 1-3% of a $500 account.
5. **It decides on hourly noise.** Hourly closes against a 50-day average are
   what made the trend exit churn: 109 trades, 34% of them winners.
6. **Its good numbers rest on a few windows.** The windows are nested, and
   the rolling test shows the median 90-day window lost 1.3% and beat hold
   only half the time.

### The rule

`strategy/breakout.py`, backtested as one account across every symbol by
`portfolio_backtest.py`. Each part answers one point above:

| Critique | The breakout's answer |
|---|---|
| 1. Caps winners, holds losers | No profit target. A **chandelier stop** 3 x ATR(20) under the highest close since entry trails winners up and cuts losers. |
| 2. Buys weakness | **Buys strength**: a daily close above every close of the previous 20 days, and above the 100-day average. |
| 3. Ignores volatility | The stop and the size are both in **ATR units**, per coin. |
| 4. Tiny, fixed size | **Sized by risk**: each trade risks 1% of the account at its initial stop, capped at 10% of the account per coin (today's `max_position_pct_of_portfolio`). |
| 5. Hourly noise | Decides **once a day** on UTC daily closes, and enters and exits on different conditions, so it cannot flip on alternate days. |
| 6. Fragile evidence | Judged as one account against **equal-weight hold of the same coins with the same money**, on expectancy in R, return over drawdown, rolling windows, and with its best coin removed. |

Once a day, on each coin's daily close:

- **Enter** a coin that is not held when its close is above the highest close
  of the previous 20 days, and above its 100-day average.
- **Size** it so a stop 3 x ATR(20) under the close would lose 1% of the
  account; at most 10% of the account in one coin; nothing under $5.
- **Exit** when the close is more than 3 x ATR(20) under the highest close
  since entry. Sell it all. No profit target, no adding to a position.

Every fill crosses half the 1.9% round trip at the daily close.

It is a trend-following rule, and its bet is that these coins trend on a
daily scale strongly enough to pay for the false breakouts. On a synthetic
market that does not trend, it loses slowly (about −0.4R a trade, with a far
smaller drawdown than holding). On a synthetic trend, it catches most of the
move in one trade. Which of those real 2024-26 prices looked like is what
the backtest answers.

### Running it

```powershell
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 730 --roll-window 90
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 365
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 180
```

The `--symbols` flag reaches past the watchlist on purpose: a backtest reads
Coinbase history and trades nothing. `--risk-pct` and `--max-weight-pct`
change only the size. The edge per trade (R) does not move with them; the
account's return and drawdown scale with them together.

### The bar it has to clear

Fixed on 2026-09-27, before any run on real bars. At the default size, on
BTC, ETH, SOL and XRP together:

1. **Edge.** Over the 730-day window, at least 30 closed trades with an
   expectancy above **+0.2R** after the 1.9% round trip, and the stressed
   (2.85%) run still makes money.
2. **Risk.** Over the 730-day window, a max drawdown no more than **half**
   of equal-weight hold's, and a better return over max drawdown than hold.
3. **The bad year.** Over the 365-day window, it makes money, or loses less
   than half of what hold loses.
4. **Consistency.** In the 90-day rolling windows, it makes money in more
   than half, and its worst window loses no more than half of hold's worst.
5. **Breadth.** It still makes money with its most profitable coin left out.

It trades live only if it clears all five, and even then only as a deliberate
change: a live implementation behind the same 14 risk rules and approve-by-id
gate, sized within `config/risk_limits.yaml`. If it fails, the honest
conclusion is that none of these rules earns back a 1.9% round trip at this
size.


### The breakout, tested

On 2026-09-27, on real Coinbase bars for BTC, ETH, SOL and XRP, one $500
account at the default size (1% risk per trade, 10% cap per coin):

| | 180d | 365d | 730d | 1460d |
|---|---|---|---|---|
| breakout return (stressed) | +5.2% (+4.3%) | −0.0% (−1.2%) | +30.6% (+26.9%) | +58.1% (+49.5%) |
| breakout max drawdown | −4.6% | −8.6% | −16.2% | −16.1% |
| closed trades, expectancy | 6, −0.65R | 10, −0.96R | 31, **+0.67R** | 65, **+0.74R** |
| hold return | +25.8% | −38.4% | +36.2% | +191.7% |
| hold max drawdown | −30.3% | −63.7% | −64.9% | −63.7% |

The 1460-day column was run on 2026-09-28; it is scored under
[Over four years](#over-four-years).

Over 730 days its winners averaged +3.62R and its losers −0.95R, with 35%
of closed trades winning and 14% of the account in coins on average. XRP
supplied $87 of its $153. Without XRP it made +12.2% at +0.11R. In the 90-day
rolling windows it made money in 8 of 22 and beat hold in 12. Its median
window lost 2.0%, its worst 4.8% (hold's worst lost 37.9%), and its best made
31.1%. In the 180- and 365-day runs every closed trade lost, and the four
positions still open at the end held the gains.

Against the bar:

1. **Edge: passes.** It had 31 closed trades at +0.67R, and the stressed
   run made +26.9%.
2. **Risk: passes.** Its drawdown was −16.2% against hold's −64.9%, and its
   return over drawdown 1.89 against 0.56.
3. **The bad year: passes.** It made −0.0% while hold lost 38.4%.
4. **Consistency: fails.** It made money in 8 of 22 windows, not more than
   half, though its worst window (−4.8%) passes.
5. **Breadth: passes.** Without XRP it still made +12.2%.

**It does not clear the bar, so it does not trade live.** It is still the
first rule here with a positive expectancy after costs, and its risk profile
is far better than hold's. Two cautions go with that:

- **31 trades is a small sample.** A few large winners carry the average, so
  +0.67R is suggestive, not proven.
- **XRP carries it.** Without XRP the edge per trade (+0.11R) is below the
  +0.2R the bar asks for.

The next evidence is the same rule, unchanged, over more history (runbook,
"Backtesting").

### Over four years

On 2026-09-28, the same rule, unchanged, over 1,460 days (2022-09-30 to
2026-09-27), on the same four coins and the same $500 account. The first two
of those years were prices it had never been run on.

It closed 65 trades and won 37% of them: winners averaged +3.64R, losers
−0.96R, with 15% of the account in coins on average. Every coin made money:

| coin | closed trades | net P&L | expectancy |
|---|---|---|---|
| BTC-USD | 17 | $49.98 | +0.62R |
| ETH-USD | 17 | $10.15 | +0.03R |
| SOL-USD | 17 | $132.94 | +1.18R |
| XRP-USD | 14 | $97.44 | +1.22R |

Without SOL, its most profitable coin, it made +27.6% at +0.59R on 48 closed
trades. In 37 rolling 90-day windows it made money in 17 and beat hold in 15.
Its median window lost 0.8%, its worst 5.7% (hold's worst lost 38.6%), and its
best made 41.0%. Its worst drawdown in any window was 10.1%.

Against the bar, applied to the four-year window:

1. **Edge: passes.** 65 closed trades at +0.74R; the stressed run made +49.5%.
2. **Risk: passes.** Its drawdown was −16.1% against hold's −63.7%, and its
   return over drawdown 3.61 against 3.01.
3. **The bad year: passes.** Unchanged: the last 365 days are the same bars.
4. **Consistency: fails.** It made money in 17 of 37 windows; more than half
   is 19. Its worst window (−5.7%) passes.
5. **Breadth: passes.** Without SOL it still made +27.6%.

**Still 4 of 5, so it still does not trade live.** The evidence is stronger
than at two years: twice the trades at the same edge, every coin positive,
and the breadth check well above the +0.2R the edge asks for. By subtraction,
the trades from the two unseen years averaged roughly +0.8R. Consistency is
the rule's nature more than bad luck: with 37% of trades winning, most 90-day
windows lose a little and a few make a lot. And hold made far more money over
these four years (+191.7% against +58.1%), at four times the drawdown.

The next evidence is live prices: [the forward test](#the-forward-test).

## The split: long-term + short-term

On 2026-09-28 the owner asked whether the $500 account could run $250 on a
long-term play ("buy low, HODL") and $250 on short-term trades.
`rhca backtest --strategies split` answers it on history; the
[forward test](#the-forward-test) answers it on live prices.

### The two sleeves

One account in two sleeves that never pass cash between them and are never
rebalanced.

- **Long-term** (`strategy/hodl.py`) never sells. Its money is split evenly
  across its coins, and there are three ways to buy it, all fixed before any
  result was seen:
  - **buy low** (`dip`, the default): one of ten equal tranches of each coin
    at most once a week, and only on a daily close *under* the coin's 200-day
    average. While a coin trades above it, the cash waits -- in a market that
    only rises, it never buys.
  - **weekly DCA** (`dca`): one tranche a week whatever the price, for ten
    weeks.
  - **lump sum** (`lump`): everything on the first day -- the hold baseline.
- **Short-term** is the breakout, sized off its own sleeve: 1% of the
  sleeve at risk per trade, at most 10% of the sleeve in one coin. Not the
  trend ladder, which lost money over 365 and 730 days and failed its bar.

### Running it

```powershell
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 1460 --roll-window 90 --strategies split
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 730 --roll-window 90 --strategies split
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 365 --strategies split
```

The report shows the long-term sleeve bought all three ways, the breakout
sleeve, the split, and the whole account in the breakout or in hold -- each
at the normal and stressed round trip, with its return, max drawdown, return
over drawdown, and share in coins. `at end` on a long-term row is the share
it managed to buy: the rest is cash that never met its rule. The rolling
windows show the split and each sleeve against hold.

`--long-mode dca` or `lump` changes how the split's long-term sleeve buys;
`--long-pct 30` puts 30% there instead of 50%; `--long-symbols
BTC-USD,ETH-USD` holds only those two long-term. The 200-day average's
warm-up is fetched before the window.

### What running it for real would take

Nothing trades either sleeve today: neither has a live implementation, and
the ladder is the only rule that proposes. A live split needs each of these,
as a deliberate decision by the owner:

1. **The breakout clearing its bar**, or the owner choosing, on the record,
   to trade it at 4 of 5.
2. **Room under the concentration cap.** `max_position_pct_of_portfolio` is
   10%: $50 of any one coin in a $500 account, counting everything the account
   holds in it, whichever sleeve bought it. Long-term on four coins is $62.50
   each (12.5%), and the breakout can add up to $25 (5%): 17.5% in one coin.
   Long-term on BTC and ETH alone is $125 each (25%) plus the breakout's $25:
   30%, over the 25% ceiling in `config.py`.
3. **The watchlist.** It lists BTC and ETH; SOL and XRP would have to join it.
4. **A live implementation of each sleeve**, behind the same 14 risk rules
   and approve-by-id gate. The trade limits already fit: a long-term tranche
   is $6.25 and a breakout entry at most $25, inside the $5 minimum and $50
   maximum.

## The forward test

`config/shadow.yaml` sets it up: from the 2026-09-28 daily close, `rhca run`
keeps the split on paper -- $250 bought the buy-low way and $250 in the
breakout, on BTC, ETH, SOL and XRP. After each UTC daily close it replays the
paper account from the start on Coinbase's daily closes, and writes one
`shadow_day` record to the audit log: the paper fills of that close, and
Robinhood's bid and ask for each coin traded, read at that moment.
`rhca shadow` replays it again and reports against the bar below.

It never proposes and never orders. A `shadow_day` record has no proposal
id, `rhca approve` cannot find it, and no code path leads from it to an
order. Replaying from the start each day keeps the paper account identical to
what `rhca backtest --strategies split` computes for the same days: one
implementation of each rule, and no carried state to drift.

Changing `config/shadow.yaml` restarts the test.

### The bar

Fixed on 2026-09-28, before any forward close existed. It is read after 90
daily closes: the 2026-12-26 close, available on 2026-12-27.

1. **Faithful.** Every day `rhca run` recorded matches the replay: the same
   fills on the same close. A day it was not running is not counted, so
   leave it running.
2. **Costs.** The median Robinhood spread at paper-fill time is at most 1.9%,
   the round trip every backtest here charges.
3. **In range.** Over those 90 closes the breakout sleeve returns at least
   −5.7%, with a max drawdown of at most 10.1%: no worse than its worst 90-day
   window in the four-year backtest.

Passing all three says the split behaved live as it did in its backtest, at
Robinhood's real cost. It does not undo the breakout's consistency failure,
and it authorizes nothing: trading the split with real money stays the
owner's decision, with everything under
[What running it for real would take](#what-running-it-for-real-would-take).
Failing any check means stopping to find out why, before anything else.
