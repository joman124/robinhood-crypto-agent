---
name: rhca-forward-test
description: >-
  Read the forward test, the split on paper since the 2026-09-28 close, against
  its bar (fixed 2026-09-28) and the exit criteria for a quiet market, with
  `rhca report` (offline) and `rhca shadow` (the full report and the three
  checks). Use when the owner asks "how is the forward test doing?", "paper
  results", "shadow report", "did it pass the bar?", "how far is each coin from
  a trade?", "is the test inconclusive?" or "what would the paper trades have
  made?".
---

# The forward test

`rhca run` keeps the split on paper beside the live account and records one
`shadow_day` after each UTC daily close. It never proposes and never orders.
This skill reads that record against the bar in `docs/strategy.md` ("The
bar") and the sample rules in `docs/forward-test-exit-criteria.md`. It changes
nothing.

```
rhca status     # the loop must be RUNNING for a close to be recorded
rhca report     # the glance, from the audit log: no price fetch, no broker
rhca shadow     # replays Coinbase's daily closes: fills, distances, the bar
```

## The bar

Fixed on 2026-09-28. Read after 90 closes: the 2026-12-26 close, on 2026-12-27.

| Check | Passes when |
|---|---|
| 1. Faithful | Every day `rhca run` recorded matches the replay: the same fills on the same close. |
| 2. Costs | The median Robinhood spread at paper-fill time is at most 1.9%. |
| 3. In range | The breakout sleeve returns at least -5.7%, with a max drawdown of at most 10.1%. |

A **priced fill** is a paper fill logged live with Robinhood's quote.
`rhca shadow` marks it `logged live, Robinhood spread X%`, and check 2 counts
them: `X% over N fill(s)`.

## Steps

1. Run `rhca status`. Quote the `pipeline` line and the `forward test` lines.
   A loop that is `NOT RUNNING` misses closes, and a missed close never gets a
   Robinhood price.
2. Run `rhca report`. Quote `closes recorded`, `paper fills`, `split equity`,
   `hold equity` and `max drawdown` exactly. Its `max drawdown` is the whole
   split's, not check 3's breakout sleeve. Its `pass rate by coin` counts live
   proposals that passed risk: not a win rate, and not paper fills.
3. Run `rhca shadow`. It fetches Coinbase's daily closes. If that fails, say
   so and report only what `rhca report` printed (CLAUDE.md rule 10).
4. Quote the `N close(s) so far` line and the `rhca run recorded` line, with
   any closes it missed.
5. Quote each check and its bracketed verdict exactly: `[pass so far]`,
   `[FAIL so far]` or `[-]`, and after 90 closes `[pass]` or `[FAIL]`. Then the
   conclusion line, if one is printed. After "All three pass", its second
   sentence predates the split going live on 2026-09-28: quote it, and say
   the split already trades.
6. Count the priced fills: check 2's `N`, or 0 when it prints
   `no paper fill with a Robinhood quote yet`. Then apply the exit criteria,
   and say they are the exit criteria's verdict, not the tool's:
   - **Before 90 closes:** every verdict is provisional. Say how many closes
     are left.
   - **At 90 closes, 3 or more priced fills:** the bar is scored as printed.
   - **At 90 closes, fewer than 3:** the verdict is
     **inconclusive-no-trigger**, whatever the conclusion line says. "All
     three pass" on 0 to 2 priced fills is not a pass.
   - **One extension, to 180 closes** (the 2027-03-26 close; the exit-criteria
     doc prints the year as 2026). With 3 priced fills by then, score the bar.
     Without, close the test with a documented no-trigger verdict. Nothing
     about the live account changes.
7. A check that fails at the read means stopping approving the split's
   proposals until the reason is found (`docs/strategy.md`, "The bar"). Tell
   the owner; that decision is theirs.
8. Quote the "How far each coin is from a trade" lines as printed. They hold
   until the next close.
9. "Had every paper trade that passed risk been approved" is hypothetical.
   Quote its `taken` and `not taken` lines, and label them so.

## Rules

- Repeat every count, percentage, verdict and conclusion exactly as printed.
  Never round, re-total or reword them.
- A paper fill is not a proposal: it has no proposal id, and nothing can
  approve it. A passing risk verdict on a paper fill is not approval either
  (CLAUDE.md, "Your role").
- The bar was fixed before any forward close existed. Never move a threshold,
  the read date or the 3-fill floor after seeing numbers, and never read a
  provisional verdict as final.
- The 90-close bar tests whether the live path behaves like the backtest. It
  does not re-prove the breakout's edge: that needs 30 or more closed trades,
  about two years.
- Changing `config/shadow.yaml`, or `max_position_pct_of_portfolio`, restarts
  the test (rule 9). Only the owner changes either.

## What it never does

- Suggests placing, approving or sizing an order from a paper fill or the
  what-if.
- Calls fewer than 3 priced fills a pass, or a failed check "close enough".
- Edits or deletes `config/shadow.yaml`, or any `config/*.yaml`.
- Runs `rhca shadow --backfill` unasked. It appends `shadow_day` records for
  missed closes with no paper fill, and that is the owner's call (runbook,
  "Recording missed closes").
- Starts or stops `rhca run`.
