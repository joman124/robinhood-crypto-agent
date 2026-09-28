# Roadmap and handoff

Where the project stands, and what the next session should pick up.

## The architecture (decided 2026-09-28)

```
[ Coinbase daily closes ] + [ Robinhood quotes ]
              |
              v
[ The split ]   BTC, ETH, SOL, XRP: $250 bought a tranche at a time under the
              |  200-day average and held; $250 in the breakout, stopped out on
              |  a trailing ATR stop; no coin over 20% of the account
              v
[ 14 risk rules ] -> proposal -> a human approves by id -> Claude places it via MCP
```

It replaced the trend ladder on 2026-09-28, by the owner's decision with the
breakout at 4 of its 5 criteria (`strategy.md`, "What going live took"). The
ladder had replaced the System 1 / System 2 pipeline (indicators and Jev news labels,
an escalation trigger, Claude Sonnet 5 as a second opinion) at the owner's
request. The evidence that led here is in [`strategy.md`](./strategy.md#history).
In short: at Robinhood's ~1.9% round trip, a six-hour hold has to move more
than a typical six-hour move just to break even, and System 1's record matched
that. The dip ladder, filtered by the 50-day average, was the only variant
that beat holding per dollar on BTC and ETH across the 180, 365 and 730-day
windows -- until its trend exit was tested on real bars and failed its bar.

## Where things stand

- **Live:** the split (`strategy/split.py`), in `rhca run` and `rhca analyze`,
  on Coinbase's daily closes, replayed by `rhca backtest --strategies split`,
  with a test holding the two to the same trades. Both sleeves' state comes
  from recorded fills (`ledger.py`). `rhca status` shows each sleeve's cash,
  holdings and realized P&L.
- **On paper beside it:** the forward test (`shadow.py`), read with
  `rhca shadow`.
- **Not yet evidenced live:** anything. The breakout's 4-year backtest
  passes 4 of 5; nothing has filled yet.

## Next

0. **The split is live** from 2026-09-28 (`strategy.md`, "The split, live").
   Every proposal is approved by id; the first ones will be long-term
   tranches on coins under their 200-day average, and any breakout entries.
1. **Keep `rhca run` running.** It proposes the split and records the
   forward test. Read the forward test's bar with `rhca shadow` on or after
   2026-12-27; a failed check means stopping approving the split's proposals
   until the reason is found.
2. **Record every fill and cancellation.** Each sleeve's cash and holdings are
   only as good as the audit log. Reconcile against `get_crypto_orders` weekly
   (runbook, "Reconciling against Robinhood").
3. **Measure it live on P&L**, from `rhca status`, over months. A breakout
   comes a few times a quarter per coin, so the live evidence comes slowly;
   the backtest and the forward test are the fast evidence.
4. **Any holding left from the retired ladder** is in neither sleeve and is
   never sold by the split; sell it by hand if you want it gone. It still
   counts toward the 20% per-coin limit.

## Done: dashboard UX (2026-09-23)

Every item on the old list shipped: the pipeline's fields and a stage filter,
`by_status` accuracy, Accept greyed out past `price_drift_tolerance_pct`,
relative timestamps, the signal column explained, per-signal rationale,
single-row accuracy panels collapsed, a hit-rate trend, pagination,
pair/outcome filters and a mobile card layout. The payload (version 2) also
gained the kill switch, the loop heartbeat, the last mark per pair, per-rule
risk verdicts and an execution summary. See [`dashboard/README.md`](../dashboard/README.md).

What stays out, deliberately: the text of risk messages that quote the
account, and the loop's last error text. Read those locally with `rhca audit` and
`rhca status`.

Still open: the loop only syncs when `RHCA_DASHBOARD_URL` and
`RHCA_DASHBOARD_TOKEN` are set where it runs.

## Carried over

- **Phase 2, unattended execution.** The shadow run is the evidence phase. The
  promotion checklist and the missing machinery are in
  [`autonomy.md`](./autonomy.md): an auto-approval policy, a dead-man's
  switch, per-cycle reconciliation and a tested rollback. The order call
  belongs behind `ApprovalGate`, never in `robinhood.py` directly.
- **joman124/robinhood-crypto-agent#1** is still open and superseded. Closing
  it is the owner's call.

## What not to erode

1. **The dashboard never places orders and holds no Robinhood credentials.**
   Accepting records a decision; the agent replays it through the full gate.
2. **`OVERRIDE RISK CHECK` stays typed-only.**
3. **Config can only tighten.** Raising a ceiling is a code change.
4. **The rule the agent trades is the rule the backtest replays.** One
   function, and a test that holds the two to the same trades. A live-only
   tweak would make every backtest evidence about something else.
5. **The split's state comes from recorded fills**, never from memory, so it
   survives a restart and never sells a coin it did not buy.
6. **The Robinhood client stays read-only until Phase 2.**
7. **A failure is never an approval.**
