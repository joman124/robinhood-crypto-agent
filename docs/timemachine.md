# The Time Machine: a trade's odds, replayed on past days

`rhca timemachine` asks what usually happens next. It replays the coming days
thousands of times from a coin's own past daily bars, runs the split's rule
over each replay, and sets the rule's real record on that coin beside the
replays. It is borrowed from the Time Machine in
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT),
which does the same for intraday stock trades, and adapted to a rule that
decides on daily closes and pays a 1.9% round trip.

**Nothing here trades.** It writes no proposal and no audit-log record, and a
good replay is not approval (CLAUDE.md, "Your role"). Like the forward test's
paper fills, it is information for the owner's decision on the dashboard.

## What it answers

```powershell
.venv\Scripts\rhca timemachine --proposal <id>   # the breakout entry a proposal is for
.venv\Scripts\rhca timemachine SOL-USD           # a breakout entry at the last close
.venv\Scripts\rhca timemachine --forward-test    # will the forward test get enough fills?
```

**A breakout entry.** If the breakout buys this coin at this close, with its
stop 3 x ATR(20) under the highest close since entry:

- how often the stop sells it by day 30, 60 and 90, at a gain and at a loss
  (a trailing stop can sell above the entry);
- how often it is still held at day 90;
- what the trade is worth in R when the stop or day 90 comes: mean, median,
  the 5-95% range, and how often it is above zero;
- the price on day 90;
- **the history**: the same rule on the coin's real closes over the days the
  replays drew from, with the entries it made, how many won, and their mean R.

Then one verdict line on the mean R: close to break-even (under 0.1R either
way), leans one way (under 0.3R), or strongly. Under it, when they apply: the
median replay went the other way (a few large replays carry the mean); the
history disagrees with the replays; the history has fewer than five closed
trades.

Only a short-term entry can be replayed. A long-term tranche is held for good,
and a stop is a sell, so there is no trade left to run; `--proposal` says so.

**The forward test.** Its bar means something only with at least 3 paper
fills priced at a Robinhood quote ([`forward-test-exit-criteria.md`](./forward-test-exit-criteria.md),
section 2). It reports the fills so far, how many `rhca run` logged with a
quote, and for the 90-close read and the 180-close extension: how often the
replays reach 3 priced fills, how often there is no new fill, the median, and
each coin's odds of a breakout entry and of a tranche. The future fills count
as priced on the assumption that `rhca run` records every close from here.

## How a replay is made

1. **The days.** Each real day in the sample (`--sample-days`, default 1,460:
   the four years the breakout's backtest was judged on) becomes four ratios
   to the close before it: open, high, low, close.
2. **The blocks.** A replay strings real days together in blocks of
   consecutive days. Each day after the first starts a new block, at a random
   day, with chance 1 / `--block-days` (default 10), so blocks average ten
   days. Crypto's calm and wild stretches come in runs, and blocks keep them.
3. **Every coin at once.** In the forward test every coin draws the same past
   days, which keeps how the coins move together. Only days every coin has
   can be drawn.
4. **The rule's own code.** Each replayed day is scaled to the replay's last
   close and goes through `day_views` and `Breakout.exits` for a trade, and
   through `shadow.run_paper`, which `rhca shadow` also runs, for the forward
   test. A test holds a replayed trade to the exit and R the backtest
   (`run_breakout`) gives on the same bars, and another holds the forward
   test's count to `rhca shadow`'s own replay.
5. **R** is the backtest's: in at the close plus half the round trip, out at a
   close less half, over what the initial stop would have lost.
6. **The seed.** Draws come from `random.Random(--seed)`, default 1. The same
   bars, seed and settings give the same numbers on any machine; say the seed
   with every result.

`--replays` (default 2,000), `--horizon` (default 90 days) and `--round-trip`
(default 1.9%) change the run, and `--json` prints the numbers as JSON. A
2,000-replay trade takes a few seconds; the forward test, which replays the
whole paper account across every coin, takes about half a minute.

## What it is not

- **Not a forecast.** The replays are made of the coin's own past, so they
  carry its past drift (the report prints how far the coin moved over the
  sampled days), and they know nothing of today's news.
- **Not conditioned on a breakout.** The replays draw from all of the coin's
  days, not from the days after a breakout. The history is the half that
  looks only at real breakouts. When the two disagree, the report says so.
- **Not evidence of an edge.** The edge is judged by the backtest bar in
  [`strategy.md`](./strategy.md) and by live P&L. A replay changes no limit,
  no bar, and no rule.

It needs Coinbase's daily bars, cached under `data/daily/` and shared with
`rhca run`. Run it where Coinbase is reachable: the owner's PC, not a cloud
sandbox.
