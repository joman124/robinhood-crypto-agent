# The path to autonomous execution

The intended destination is an agent that trades unattended. This document is
the route: what already works without a human, what still has to be built, and
what has to be *proven* before the mode is flipped.

Autonomy is a promotion the agent earns with evidence, not a config edit.

## Where the line sits today

```
analyze ─► size ─► plan ─► risk ─► PROPOSAL ─┬─► [ HUMAN NAMES THE ID ] ─► submit
                                              │        ▲
                                              │        └── the only human-shaped step
                                              └─► audit log (always)
```

Phase 1 is everything left of the bracket, and it runs today.

## What is already autonomy-ready

This is the part worth knowing: **only one check in the whole pipeline depends
on a human existing.** Everything else is already authorization-agnostic and
already runs unattended:

| Component | Unattended today? | Why |
|---|---|---|
| Strategy, regime, sizing | Yes | Pure functions over bars |
| The 17 risk rules | Yes | Evaluated from state, not input |
| Kill switch | **Better than yes** | A file — engageable from cron, survives a crash |
| Price-drift re-check | Yes | Compares a fresh quote to the proposal |
| Remaining-quantity accounting | Yes | Derived from the audit log |
| Daily notional / loss caps | Yes | Computed from the audit log, survive restarts |
| Order-contract validation | Yes | Offline schema check |
| **Approval** | **No** | Requires a human to name a proposal id |

So Phase 2 replaces `ApprovalGate._check_approval` with a policy check. It does
not rework the pipeline, and it must not weaken anything above.

## What Phase 2 has to add

### 1. A policy authorization source

An `AutoPolicy` standing in for the human, requiring *all* of:

- `risk.passed` is true — **with no override path**, see the invariants below
- composite confidence and `|score|` above thresholds *stricter* than manual
- notional within a separate, much smaller `auto_max_notional_per_trade`
- symbol on a separate `auto_watchlist` (a subset of the manual one)
- today's auto-executed trade count below an auto-specific cap

Each of these is a reason the trade can be declined, logged by name — the same
way risk findings are today.

### 2. Something to run the loop

Claude currently calls the MCP tools from an interactive session. Unattended,
there are two options, and they are not equivalent:

- **A scheduled Claude session** (a Routine firing on a cron) that runs the
  documented ingest → analyze → policy → submit → record loop. **This is the
  recommended path**: it keeps the property that the Python package has no
  network access and no credentials, so the blast radius of a bug in this
  repository stays "a bad proposal", never "a bad order".
- **A direct Robinhood API client inside the package.** This *breaks* that
  property — the package would then hold credentials and be able to trade on
  its own. Not recommended, and it would invalidate most of the safety
  argument in `docs/architecture.md`.

**Decision, 2026-09-21.** The owner chose the second shape. A direct client
feeds a real-time System 1, and Claude Sonnet 5 is System 2 over the API; see
[`roadmap.md`](./roadmap.md). It is built *read-only* first: `robinhood.py`
holds credentials but has no order or cancel method. So today the blast radius
of a bug is still a bad proposal, and `rhca run` is the shadow-mode evidence
phase this document asks for. The order call is the Phase 2 change. It belongs
behind `ApprovalGate` with the `AutoPolicy` above, not added to the client.

### 3. A dead-man's switch

In propose-only, a loop that silently stops is harmless — you just get no
proposals. Unattended, a stopped loop can leave a staged plan half-filled with
nothing supervising it. Phase 2 needs a heartbeat: if the loop misses N
consecutive cycles, engage the kill switch and notify.

### 4. Automatic reconciliation

Reconciling the audit log against `get_crypto_orders` is a manual step in
`docs/runbook.md` today. Unattended it has to run every cycle, and any
divergence between what the log says and what Robinhood holds must engage the
kill switch rather than be reported and passed over. An agent that has lost
track of its own position must not place the next order.

## The missing evidence, and how to get it

Today there is **no measurement of whether the proposals are any good.** The
agent records them; nothing scores them. Promoting on an unmeasured strategy
would be automating an unknown.

The good news is the raw material already exists: every proposal, including
risk-blocked ones, is in the audit log with its score, confidence, regime and
reference price. What is missing is a report that joins them to what the price
subsequently did.

**This is the single most valuable thing to build next**, before any Phase 2
machinery:

```
rhca shadow-report --since 2026-08-01
  # for every logged proposal: what the policy WOULD have done,
  # and what the price did over the following N bars
  # -> hit rate, average move captured, worst case, by regime and by symbol
```

It is useful in Phase 1 on its own — it tells you whether to trust the
proposals you are signing off — and it is the evidence that earns Phase 2.

## Promotion criteria

Checkable, so the decision is not a mood:

- [ ] **Volume**: at least 30 days and 50 logged proposals of propose-only
      operation.
- [ ] **Measured edge**: `shadow-report` shows the auto-eligible subset would
      have been net positive *after fees and the real spread*, over a window
      containing at least one drawdown. A backtest of a rising market proves
      nothing.
- [ ] **Clean books**: 30 days of reconciliation with zero unexplained
      divergences between the audit log and Robinhood.
- [ ] **An affordable worst case**: auto caps set so that the worst plausible
      unattended day is a number you would shrug at. Write that number down
      first, then derive the caps from it — not the other way round.
- [ ] **Machinery shipped and tested**: heartbeat, auto-reconciliation, and
      the policy's decline paths all covered by tests.
- [ ] **A tested rollback**: flipping back to `propose_only` mid-flight, with
      open staged orders outstanding, without orphaning them.

## Invariants that do not change in Phase 2

These hold in autonomous mode exactly as they do now:

1. **`OVERRIDE RISK CHECK` stays human-only.** An autonomous agent must never
   be able to override its own risk engine — that is the difference between a
   limit and a suggestion. In auto mode a blocked proposal is simply declined.
2. **The kill switch stays fail-safe**, and unreadable still means engaged.
3. **The audit log stays append-only and stays load-bearing** for the daily
   caps.
4. **Order payloads stay contract-validated** before submission.
5. **Caps only tighten by config; loosening stays a code change** against the
   ceilings in `config.py`.
6. **Auto caps stay separate from manual caps**, so tightening unattended
   trading never requires loosening supervised trading.

## The honest risks

- **Correlated loss.** The daily loss cap is enforced from *recorded* P&L, so
  it lags. Several positions moving together inside one cycle can exceed it
  before the cap sees it. Auto caps must be sized for that lag.
- **A quiet feedback loop.** An agent that trades its own price impact on a
  thin pair can look profitable in shadow and lose money live. Keep the auto
  watchlist to deep pairs.
- **Silent degradation.** A strategy that stops working produces no error — it
  produces worse trades. The heartbeat catches a *stopped* loop, not a
  *deteriorating* one. Re-run `shadow-report` on a schedule and treat a
  regime change as a reason to demote to propose-only.
- **The spread.** Robinhood's market-maker-routed crypto quotes were observed
  at ~1.87% on BTC. That is the cost of a round trip before the strategy makes
  a cent. Any measured edge has to clear it.
