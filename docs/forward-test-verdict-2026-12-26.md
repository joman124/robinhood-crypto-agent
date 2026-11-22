# Forward-test verdict sheet: the 2026-12-26 close

Prepared 2026-11-22 so the 90-close verdict takes minutes, not a rebuild.
Everything below comes from two read-only commands run on 2026-11-22 against
the live audit log: `rhca report` (log only, no network) and `rhca shadow`
(replays Coinbase daily closes, no `--backfill`, writes nothing). No order,
config or dashboard was touched.

**On 2026-12-27, re-run those two commands and update the figures in §1–§4.**
The figures here stop at the **2026-11-21 close (close 55 of 90)**, so 35
closes are still to come. The branch decision in §4 is already settled. The
bar checks in §5 are not final until close 90.

## 1. Where the test stands (2026-11-21 close)

| Measure | Value |
|---|---|
| Closes counted | **55** since the 2026-09-28 close (52 logged live; 2026-11-08 to 11-10 missed by `rhca run`) |
| Paper fills | **10**, all long-term tranches. **8 priced** at a live Robinhood quote, 2 unpriced (the missed 11-08 close) |
| Breakout fills | **0**. The short-term sleeve has never traded |
| Split (paper) | $490.77, **−1.8%**, max drawdown 2.2% |
| Long-term sleeve | $240.77, −3.7%, max drawdown 4.3% |
| Breakout sleeve | $250.00, +0.0%, max drawdown 0.0% |
| Hold benchmark | $313.76, **−37.2%**, max drawdown 39.1% |
| Priced fills, had they been approved | 8 trades, $50.00 in, worth $43.46: −$6.54 (−13.1%) |

The market fell about 37% during the test. The split lost 1.8% because 89% of
its paper money was still cash. This replaces the October picture of "zero
fills, blocked by inactivity". Since 2026-10-29 the long-term rule has fired
every week it was allowed to.

### The 10 paper fills

| Close | Coin | Amount | At close | Robinhood spread | Risk |
|---|---|---:|---:|---:|---|
| 2026-10-29 | XRP-USD | $6.25 | 1.2349 | 1.8876% | PASS |
| 2026-11-05 | XRP-USD | $6.25 | 1.2055 | 1.8901% | PASS |
| 2026-11-08 | ETH-USD | $6.25 | 2125.32 | not logged live | not checked |
| 2026-11-08 | SOL-USD | $6.25 | 86.45 | not logged live | not checked |
| 2026-11-12 | BTC-USD | $6.25 | 71369.9 | 1.8783% | PASS |
| 2026-11-12 | XRP-USD | $6.25 | 1.0402 | 1.8981% | PASS |
| 2026-11-15 | ETH-USD | $6.25 | 1834.54 | 1.8797% | PASS |
| 2026-11-15 | SOL-USD | $6.25 | 72.73 | 1.8828% | PASS |
| 2026-11-19 | BTC-USD | $6.25 | 61913.23 | 1.8796% | PASS |
| 2026-11-19 | XRP-USD | $6.25 | 0.9592 | 1.8962% | PASS |

Paper tranches held: BTC 2/10, ETH 2/10, SOL 2/10, XRP 4/10.

## 2. Each coin's distance to its trigger lines (2026-11-21 close)

| Coin | Close | 200-day avg (tranche line) | vs. line | Next tranche allowed | Breakout line | Rise to breakout |
|---|---:|---:|---:|---|---:|---:|
| BTC-USD | 60,086.00 | 72,425.75 | **−17.0%** (under) | 2026-11-26 | 80,428.31 | +33.9% |
| ETH-USD | 1,682.30 | 2,125.40 | **−20.8%** (under) | 2026-11-22 | 2,422.27 | +44.0% |
| SOL-USD | 70.16 | 88.4166 | **−20.6%** (under) | 2026-11-22 | 99.5326 | +41.9% |
| XRP-USD | 0.9302 | 1.2413 | **−25.1%** (under) | 2026-11-26 | 1.3060 | +40.4% |

- **Tranche line:** every coin is already under it, so the only limit is the
  7-day spacing. To stop the tranches, a coin would have to rise 20–33% (BTC
  +20.5%, ETH +26.3%, SOL +26.0%, XRP +33.4%).
- **Breakout line:** every coin needs a 34–44% rise. A breakout fill before
  2026-12-26 is very unlikely.
- **Stop line:** none. The breakout sleeve holds nothing, so there is nothing
  to stop out.

If every coin stays under its 200-day average, the weekly spacing allows 5 more
tranches per coin by the 2026-12-26 close. That gives BTC, ETH and SOL 7/10
each and XRP 9/10, about 30 paper fills in total. This is a ceiling, not a
forecast: it holds only if the price stays down and `rhca run` records every
close.

## 3. Closes counted

- **55 of 90** at the 2026-11-21 close. Close 90 is **2026-12-26**, and
  `rhca shadow` reads it from **2026-12-27**.
- Close 180 (the single extension) is **2027-03-26**.
  `docs/forward-test-exit-criteria.md` prints this as "≈ 2026-03-26", which is
  a typo for 2027.
- `rhca run` missed 3 closes (2026-11-08 to 11-10). `rhca report` counts 52
  closes because it reads only the logged days. `rhca shadow` counts 55 because
  it replays the missed ones. Use 55.

## 4. Which exit-criteria branch applies

The criteria (`docs/forward-test-exit-criteria.md` §3) give three branches:

| Branch | Condition | Applies? |
|---|---|---|
| Inconclusive at 90 closes | fewer than 3 priced fills by close 90 | **No.** There are already 8 priced fills |
| Closed no-trigger at 180 closes | still fewer than 3 priced fills by close 180 | **No.** It can no longer happen |
| **Score the bar ("pass" possible)** | **3 or more priced fills** | **Yes.** 8 priced fills; the third landed on the 2026-11-12 close |

On 2026-12-27 the bar is scored normally, with no extension. These are its
three checks today (all "pass so far", provisional until close 90):

| # | Check | Value at close 55 | Status |
|---|---|---|---|
| 1 | Faithful: every logged day matches the replay | 52 of 52 | pass so far |
| 2 | Costs: median Robinhood spread at fill ≤ 1.9% | 1.89% over 8 fills | pass so far, **only 0.01 points under the limit** |
| 3 | In range: breakout sleeve return ≥ −5.7%, max drawdown ≤ 10.1% | +0.0%, 0.0% | pass, **but vacuous**: the breakout has never traded |

Two caveats the verdict has to carry:

1. **The costs check is close to failing.** Every priced spread so far is
   between 1.878% and 1.898%, against a 1.9% limit. A few wider fills before
   close 90 could push the median over the limit.
2. **The in-range check measures nothing yet.** All 8 priced fills are
   long-term tranches. The criteria count "any mix" of fills toward the 3, so a
   pass is valid as written. But it says nothing about the breakout half of the
   split, which is the half the in-range check exists for.

## 5. What else the log shows (context for the decisions)

- **The live account has bought nothing.** Since 2026-09-28 the audit log holds
  689 live proposals, all long-term tranches (BTC 215, ETH 237, SOL 237), all
  passing risk, all still `proposed`. It holds no accept, no order and no fill.
  The only decision on record is one decline, made 2026-10-07. The first live proposal is from 2026-11-12, two weeks after the first paper tranche (XRP, 2026-10-29). `rhca status` shows both live sleeves
  at $250.00 cash. The paper account bought 10 tranches over the same closes.
- **XRP has no live proposal since the test began**, although XRP was the first
  coin under its line on paper. The audit log and `data/rhca-run.log` do not
  say why. Find the reason before relying on the live split for XRP.
- **The loop is up** (`rhca status`: running, kill switch released,
  `propose_only`). Two priced fills were lost to the 2026-11-08 to 11-10 outage,
  and a quote error occurred on 2026-11-19.

## 6. Decisions for 2026-12-27, and what each answer changes

Re-run `rhca shadow` and `rhca report` first. Then:

**Decision 1: Does the bar pass?**
- *All three checks pass:* the forward test is **passed** under the criteria as
  written. Changes: it stops being a pending signal. Nothing changes in the
  live account, and no order follows from it.
- *Any check fails* (most likely the costs median over 1.9%): per
  `docs/roadmap.md` step 1, **stop approving the split's proposals** until the
  reason is found. Changes: the live split pauses in practice, and the next work
  is diagnosis, not trading.

**Decision 2: Does a pass with zero breakout trades count for the breakout?**
- *Yes, take the pass as a whole:* the split is treated as proven on paper.
  Changes: nothing further is pending from this test.
- *No, pass the long-term half only:* keep the in-range check open until the
  breakout has 3 priced fills, or until close 180 (2027-03-26), whichever comes
  first. Changes: the forward test keeps running for the breakout only. No
  config edit is needed, because `rhca run` keeps recording it either way.
  (Editing `config/shadow.yaml` would restart the test. Don't.)

**Decision 3: What to do with the live long-term proposals.** The paper test
says the tranche rule behaves as designed. The live account has not followed
it, because no proposal was accepted.
- *Start accepting tranches by id:* the live account begins to track the
  paper one, with real money at $6.25 per tranche. Every accept is a separate
  approval of a named proposal id on the dashboard.
- *Keep declining or ignoring them:* the live account stays all cash. The
  live-vs-paper gap widens, and the "once it trades" evidence in
  `docs/autonomy.md` never starts.

**Decision 4: Fix the gaps the test exposed?** (Each would be its own task.
None changes the verdict.)
- Find out why the live loop proposes no XRP tranche. If left alone, the live
  split never buys XRP while the paper one does.
- Harden against missed closes like 2026-11-08 to 11-10. If left alone, more
  paper fills go unpriced and drop out of the costs check.

**Not on the table:** promoting to unattended (`auto`) execution. A pass here
is one input to `docs/autonomy.md`'s promotion criteria. The live-ledger,
reconciliation and machinery criteria are still unmet, so a pass does not
unlock Phase 2.
