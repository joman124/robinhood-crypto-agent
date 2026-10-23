# Candidates: faster rules, and the cost each one needs

The split trades on daily closes because, at Robinhood's ~1.9% round trip,
nothing faster has paid: System 1's six-hour holds won 0 of 52 decided sells,
and the ladder's hourly trend exit churned its upside away
([`strategy.md`](./strategy.md#history)). A faster bot is only worth building
if a rule clears its bar at a cost the account can actually get. This document
fixes the candidates and that bar, on 2026-10-23, before any of them has been
run on real bars. `rhca backtest-candidates` runs them.

**Nothing here trades.** Every rule below is backtest-only. A candidate that
clears its bar authorizes nothing: trading it is a deliberate change by the
owner, behind the same 14 risk rules and approval gate, and anything faster
than a daily close needs Phase 2 first ([`autonomy.md`](./autonomy.md)).

## The question each run answers

Not "does it make money", but **at what round trip it starts to**. Every
candidate runs at each of several round trips (default 1.9%, 1.0%, 0.5% and
0.2%) and is scored against the whole bar at each one. The report gives, per
candidate, the highest swept cost at which its expectancy is still positive,
and the highest at which it clears every check.

That turns the open cost question into a lookup. Robinhood's v2 order
endpoint offers fee tiers -- an explicit fee, quoted at 0.03%-0.85% by 30-day
volume, with a maker rate for an order that rests on the book -- in place of
the spread v1 prices in. Nobody has measured what a $500 account would pay
there yet. Whatever it turns out to be, read the column nearest it.

## Costs, and how fills are modelled

- **Taker** (the default): every fill crosses half the round trip at the
  close of the bar that decided it, as every backtest here does.
- **Maker entries** (`--maker-pct P`): an entry rests as a limit at the
  deciding bar's close, paying `P`% instead of half the round trip. It fills
  only if a later bar trades *below* the limit -- touching it is not enough
  -- within `--maker-bars` bars (default 3), and is canceled otherwise.
  Breakouts that run away at once are therefore missed, and the fills come on
  pullbacks: the adverse selection a resting order really gets. Exits are
  always taker, since a stop is a taker order.
- **Stressed**: every cost at 1.5x.

Equity is marked at the bid (the close less a taker half) on every bar, so a
candidate's drawdown is measured bar by bar. Hold's, the baseline, is marked
on daily closes.

## The candidates

Every candidate trades one account from $500, is long only, and decides on
closed bars. Sizing is the breakout's: a trade risks 1% of the account (or of
its sleeve) at its initial stop, and never puts more than 10% of it in one
coin, nothing under $5. "The daily trend is up" means the previous UTC day's
close was above its 100-day average: the day that has finished, never the one
still forming.

The coins are the watchlist (BTC, ETH, SOL, XRP) unless a candidate says
otherwise.

| | Candidate | Bars | Rule | Plateau neighbours |
|---|---|---|---|---|
| ref | **breakout (live)** | daily | The live rule, unchanged: enter a close above the prior 20-day high and the 100-day average; exit 3 x ATR(20) under the highest close since entry. | 10- and 30-day highs |
| 1 | **cost filter** | daily | The live rule, entering only when its stop (3 x ATR) is at least 4 round trips away, so the trade has room to pay for itself. | 2 and 6 round trips |
| 2 | **breadth** | daily | The live rule on nine coins: BTC, ETH, SOL, XRP, DOGE, LTC, AVAX, LINK, BCH. Trend following earns in lumps; more coins should smooth the lumps out. | 10- and 30-day highs |
| 3 | **blend 10/20/55** | daily | Three sleeves of a third each, trading the live rule on 10-, 20- and 55-day highs. One parameter becomes an average over three. | none: it is the average |
| 4 | **4h breakout** | 4-hour | Enter a 4h close above the prior 20 bars' closes while the daily trend is up. Exit 3 x ATR(20 bars) under the highest close since entry, or after 30 bars (5 days). | 10- and 30-bar highs |
| 5 | **day breakout** | hourly | Each UTC day, the level is the day's open plus 0.5 x the previous day's range. Enter on the first hourly close above it while the daily trend is up (never on the 23:00 bar). Exit on an hourly close under the day's open, or at the day's last close. | k = 0.25 and 0.75 |
| 6 | **dip in uptrend** | hourly | Enter an hourly close 2 standard deviations under its 20-bar mean while the daily trend is up. Exit when a close gets back to the mean, on a close 3 x ATR(20 bars) under the entry, or after 24 bars. | 1 and 3 deviations |
| 7a | **day breakout, 13-17 UTC** | hourly | Candidate 5, entering only on bars that start 13:00-16:00 UTC, the US and European overlap. | k = 0.25 and 0.75 |
| 7b | **dip in uptrend, 13-17 UTC** | hourly | Candidate 6, the same hours. | 1 and 3 deviations |

Two choices made to keep the parameter count down, against the first sketch
of these ideas: the dip rule has no ADX filter, and the hours filter is one
fixed window, not a sweep over hours (a sweep would find the best hours of
the past by construction).

The nine coins in candidate 2 are on Coinbase and, as far as known, on
Robinhood; check `get_currency_pairs` before any could trade. Choosing coins
that still trade today is a survivorship bias the result inherits.

## The bar

Fixed on 2026-10-23, before any run on real bars. Over 1,460 days, at each
round trip swept, a candidate clears the bar only if it passes all six:

1. **Edge.** At least 30 closed trades for a daily candidate, 100 for a
   faster one; an expectancy above **+0.2R** after costs; and the stressed
   run still makes money.
2. **Risk.** A max drawdown no more than half of equal-weight hold's, and a
   better return over max drawdown than hold's.
3. **The bad year.** Over the last 365 days it makes money, or loses less
   than half of what hold loses.
4. **Consistency.** In the 90-day rolling windows (a new one every 30 days),
   it makes money in more than half, and its worst window loses no more than
   half of hold's worst.
5. **Breadth.** It still makes money with its most profitable coin left out.
6. **Plateau.** Its expectancy stays above zero at both neighbours in the
   table, at the same cost: a result that lives on one parameter value is a
   fit to the past, not an edge.

Checks 1-5 are the breakout's bar ([`strategy.md`](./strategy.md#the-bar-it-has-to-clear));
6 is new, because these rules have not been run before and the breakout's
was. A check that could not be run (no rolling windows fit, say) leaves the
verdict **incomplete**, never passed.

The breakout scored 4 of 5 on checks 1-5 at 1.9%, failing consistency, and
the owner took it live anyway (2026-09-28). The reference row shows the same
rule through this engine, so every candidate is read against it.

## Running it

```powershell
.venv\Scripts\rhca backtest-candidates --days 1460
.venv\Scripts\rhca backtest-candidates --days 1460 --round-trips 1.9,1.0,0.5,0.2,0.1
.venv\Scripts\rhca backtest-candidates --days 1460 --maker-pct 0.25 --maker-bars 3
.venv\Scripts\rhca backtest-candidates --days 1460 --candidates ref,4,5 --json
```

It reads Coinbase's hourly bars, cached under `data/backtest/` and shared
with `rhca backtest`, and nothing else: no broker, no account. Four years of
hourly bars for nine coins is about a thousand requests, so a run that fetches
is slow; a run within six hours reads the cache. A coin that fails to load is
named, and every candidate that wanted it says it ran without it; run again to
fetch it. Run it where Coinbase is reachable: the owner's PC, not a cloud
sandbox.

## Results

None yet. Record them here, dated, with the command that produced them, and
read every candidate against its bar as fixed above -- not against a bar
moved after the numbers were seen.
