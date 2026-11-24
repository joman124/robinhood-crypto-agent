# The open live proposals: which are stale, and what an approval would do

Audit for task t271, read-only. Sources: `data/audit_log.jsonl` up to the
2026-11-24 02:00 UTC hourly cycle, the cached quotes in
`data/market_state.json` (observed 2026-11-24 02:24:58 UTC), and the code on
`main` at `746f602`. Nothing was approved, declined or placed, and no setting was
changed. This goes with the verdict sheet in PR #42
(`docs/forward-test-verdict-2026-12-26.md`), and Decision 3 in that sheet
depends on it.

## 1. What is open

| | 2026-11-22 13:00 UTC (the verdict sheet's count) | 2026-11-24 02:00 UTC |
|---|---|---|
| Live proposals, status `proposed` | 689 (BTC 215, ETH 237, SOL 237) | 803 (BTC 253, ETH 275, SOL 275) |
| Accepts, executions, order-desk notes | 0 | 0 |

All 803 are long-term tranche buys of $6.25 each ($5,018.75 in total if every
one were sent). Every one passed risk. The first was logged on 2026-11-12.
`rhca run` adds three every hour, one per coin. Status is written once, when
the proposal is logged, and the log is append-only. So no proposal ever
expires or changes status, and the count only grows.

## 2. How many are stale

| Gate | Where it is enforced | Proposals past it now |
|---|---|---|
| Superseded: not the newest for its coin, sleeve and rule | order desk, dashboard button | **800 of 803** |
| Older than `MAX_PROPOSAL_AGE` (75 min) | order desk, dashboard button | **800 of 803** (the same 800) |
| Price drift over 0.5% (`price_drift_tolerance_pct`) against the 02:24 mark | order desk and `rhca approve` (both through `ApprovalGate.clear`) | **752 of 803** |

Only the newest three are within age and current: `a0dabf3842e0` (BTC),
`cf390449470d` (ETH) and `029595979e86` (SOL), logged 02:00. **All three fail the
drift gate** (BTC 1.59%, ETH 1.31%, SOL 1.47%). The 51 that pass drift right now
(BTC 11, ETH 13, SOL 27) are old ones whose price happens to line up with
today's.

## 3. The drift gate compares an ask with a mid

This finding matters more than the stale count.

- A long-term tranche is priced "one order at the ask". Its `reference_price` is
  the **ask** when it was logged.
- `ApprovalGate._check_drift` compares that reference with the live quote's
  **mark**, which is the mid between bid and ask.
- The spread on every one of the 803 was 1.84% to 1.92%. So at the moment each
  proposal was logged, its own quote already read **0.91% to 0.95% of drift**,
  almost double the 0.5% tolerance.

So the gate refuses a fresh buy unless the price has risen by roughly 0.45% to
1.45% since it was logged. And it passes an old proposal once the price has
risen that much since *that* proposal was logged. The gate favours stale
proposals over current ones. This was never seen in practice because no
proposal has ever been accepted (zero order-desk notes in the log).

This audit changes nothing about it. The sector's rule is to never widen the
tolerance. The likely repair is to compare like with like (the live ask against
an ask reference), and that is a code change for its own task, with the owner's
approval.

## 4. What each approval path would do with an old proposal

**Dashboard accept, sent by the order desk (`execution/desk.py`).** An old
proposal cannot get through:

1. The dashboard only shows the newest 25 proposal rows (`MAX_PAYLOAD_PROPOSALS`).
   The server action refuses an id that is not in the payload, and it refuses
   any row that is not `actionable`. A superseded row, or one over an hour old,
   is never actionable (`dashboard._desk_blocker`).
2. The desk checks everything again. It drops an accept more than 15 minutes old
   (`MAX_DECISION_AGE`), a superseded proposal, a proposal over 75 minutes old,
   and one already sent. Then it runs the gate on a fresh Robinhood quote, the
   split's book, and the daily notional cap.

Result: an old proposal is refused. Under §3, so is the current one, unless the
price has risen 0.45% to 1.45% within the hour.

**`rhca approve <id>` (the terminal path, for the MCP order tools).**
`CLAUDE.md` says the split is not traded this way: the MCP tools reach the
Agentic account, not the main account the split trades. Only that rule keeps a
split proposal off this path, and no code enforces it. Taken this way, an old
proposal can get through. `cmd_approve` loads **any** proposal id in the audit
log and calls `ApprovalGate.authorize`, which checks the kill switch, that the
approval names the id, risk, drift, and the remaining size on that one id. It
does **not** check:

- whether the proposal is superseded or how old it is;
- the split's book: sleeve cash, or an order already working on that coin;
- the daily notional cap (that is only checked when the proposal is logged);
- the dashboard's decisions. A proposal declined on the dashboard can still be
  approved here;
- how old the quote is. Without `--quote` it falls back to the cached
  `market_state.json` quote, and nothing checks when that was observed.

Right now 51 old ids would pass `rhca approve` and produce a valid order
payload. Each one is a separate $6.25 buy, so approving several stacks orders
that the order desk would never allow. The process rules still stand between
that payload and an order: a human names the id, `preview_crypto_order` runs
first, and the quote is fetched again. No code stands there.

## 5. After the 2026-12-26 verdict

Nothing in the code depends on a date, so the verdict changes none of this.

- The loop never offers an old id again. It logs each hour's trade under a new
  id.
- The dashboard never offers an old one again. It only offers the newest
  proposal per trade, and only for up to 75 minutes.
- `rhca approve` keeps accepting every one of them, indefinitely. That includes
  the 803 already open and every hourly one after them, whenever the price comes
  back within 0.5% of the proposal's ask. If Decision 3 is "start accepting
  tranches by id", these 803 open proposals are where a wrong approval would
  come from.

## 6. For the 2026-12-27 decisions

- **Decision 3 (start accepting tranches):** as the code stands, a dashboard
  accept of the current tranche will usually be refused for drift (§3). Before
  relying on it, decide how the drift reference should be measured.
- **Until that is settled:** approve only the newest proposal for a coin, and
  only within the hour it was logged. Check the id against the dashboard's
  actionable row first. Do not use `rhca approve` on an id the dashboard marks
  superseded or over an hour old.
- **Candidate fixes**, each its own task, none made here: give `cmd_approve`
  the desk's superseded and age checks; measure drift ask-to-ask for buys;
  refuse a cached quote past the risk engine's 90-second freshness limit.

## Reproducing the counts

Read-only. Run from the repo root. The superseded count only grows. The
drift-ok count moves with the price, because `rhca run` refreshes the cached
quote every cycle: a re-run at 02:27 UTC, with a newer quote, gave 49 instead of
51.

```python
import json
from decimal import Decimal as D
log = [json.loads(l) for l in open("data/audit_log.jsonl", encoding="utf-8") if l.strip()]
live = [r for r in log if r["kind"] == "proposal" and r.get("mode") == "live"]
marks = json.load(open("data/market_state.json", encoding="utf-8"))["quotes"]["value"]
newest = {(r["symbol"], r["sleeve"], r["rule"]): r["proposal_id"] for r in live}
superseded = [r for r in live if newest[(r["symbol"], r["sleeve"], r["rule"])] != r["proposal_id"]]
drift_ok = [r for r in live
            if abs(D(marks[r["symbol"]]["mark"]) - D(r["reference_price"])) / D(r["reference_price"]) * 100 <= D("0.5")]
print(len(live), len(superseded), len(drift_ok))
```
