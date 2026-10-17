# Forward-test exit criteria

The split has traded live since 2026-09-28; the forward test
(`docs/strategy.md`, "The forward test") runs the same rules on paper beside
it. Its bar (faithful / costs / in-range) is read after 90 daily closes, on
the 2026-12-26 close. That bar answers *"does the live path behave like the
backtest at Robinhood's real cost?"* — but only if trades actually happen. A
range-bound market can leave the test with **zero paper fills**, in which case
all three checks pass vacuously (`+0.0%` return, `+0.0%` drawdown, no spread to
measure) and prove nothing.

This doc is the missing decision framework: what has to trigger, how much is
enough, and when a quiet market ends the test.

## 1. Where the trade lines sit today

The split acts only on a daily close. It trades a coin on exactly two events:

- **Breakout entry** — a close *above* its breakout line (the higher of the
  prior 20-day high and the 100-day average).
- **Long-term tranche** — a close *under* its 200-day average (dip mode, one
  tranche, at most weekly, 10 per coin; 0 of 40 bought so far).

(A third event, a breakout **stop**, can only fire once a coin is held — none
is, so there is no stop line today.)

From the 2026-10-16 close (`data/shadow.json`, written by `rhca run`):

| Coin | Close | Breakout entry at | Split tranche at | Nearer line |
|------|------:|------------------:|-----------------:|:------------|
| BTC-USD | 81,500.04 | **≥ 86,507.11** (+6.1%) | **< 72,358.73** (−11.2%) | breakout, +6.1% |
| ETH-USD | 2,419.92 | **≥ 2,726.66** (+12.7%) | **< 2,144.71** (−11.4%) | split, −11.4% |
| SOL-USD | 104.32 | **≥ 121.97** (+16.9%) | **< 87.36** (−16.3%) | split, −16.3% |
| XRP-USD | 1.3096 | **≥ 1.5271** (+16.6%) | **< 1.2799** (−2.3%) | **split, −2.3%** |

Every coin sits in the dead zone between its two lines, which is why there have
been **0 proposals since the test started**. The loop is healthy; the market
simply has not crossed a line. The one line within arm's reach is **XRP's
200-day average at 1.2799** — a 2.3% dip from here fires the first tranche.
Everything else needs a 6–17% move. These lines move with each close; `rhca
shadow` reprints them, and this table should be refreshed from `data/shadow.json`
whenever the verdict is revisited.

## 2. How many triggers make a meaningful sample

Two different questions, two different answers — do not conflate them.

**For what the 90-day bar actually tests (live-vs-backtest fidelity):** the
floor is **≥ 3 paper fills that carry a Robinhood quote** (any mix of breakout
entries, stops, or long-term tranches). Below that, the "costs" check has no
median spread to compute and the "in-range" check is a vacuous `+0.0%`. Three
priced fills is the minimum that makes those two checks mean something; more is
better. **A bar that passes with fewer than 3 priced fills is "inconclusive,"
not "pass."**

**For re-proving the breakout's edge (+0.2R after costs):** that needs **≥ 30
closed trades**, the same sample the backtest bar demanded. At the four-year
backtest's rate (65 closed trades over 1,460 days ≈ 16/year across four coins),
30 closed trades is roughly **two years** of live closes. The 90-day forward
test is *not* powered to do this and must not be read as doing it. Edge
re-validation is a separate, ~2-year track.

So: the forward test's own success needs only a handful of priced fills; it was
never the thing that confirms the edge.

## 3. When a quiet market ends the test

A trigger is any paper fill: a breakout entry, a breakout stop, or a long-term
tranche.

- **First read — 90 closes (2026-12-26 close, available 2026-12-27), as already
  fixed.** If ≥ 3 priced fills have occurred, score the bar normally. If fewer,
  the verdict is **inconclusive-no-trigger**: the loop ran faithfully but the
  market never crossed a line, so the test confirms nothing and disproves
  nothing about live behaviour, cost, or edge.

- **One extension — to 180 closes (≈ 2026-03-26 close).** A single 90-day window
  can be a genuinely flat regime, so grant exactly one more window. If ≥ 3
  priced fills land in the meantime, score the bar. If 180 closes pass with
  still fewer than 3, **close the forward test with a documented no-trigger
  verdict** and stop treating it as a pending signal.

**What a no-trigger verdict means operationally:** nothing changes. The split
is already live under the 14 risk rules and the approve-by-id gate; the forward
test is only the side check. A no-trigger verdict says the check never got data
to speak, not that the strategy failed. The live account keeps running as-is,
and the forward test reopens on its own the moment a line is crossed — the next
fill restarts the clock on §2's sample count.

## Summary

| Question | Answer |
|---|---|
| What triggers a trade now | BTC breakout ≥ 86,507 / split < 72,359 · ETH ≥ 2,727 / < 2,145 · SOL ≥ 121.97 / < 87.36 · XRP ≥ 1.5271 / **< 1.2799 (−2.3%, the nearest)** |
| Meaningful sample | ≥ 3 priced paper fills for the fidelity bar; ≥ 30 closed trades (~2 yr) to re-prove the edge — out of scope for this test |
| No-trigger deadline | Inconclusive at 90 closes (2026-12-26) if < 3 fills; close with a no-trigger verdict at 180 closes (~2026-03-26) if still < 3 |
