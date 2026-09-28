# Roadmap and handoff

Where the project stands, and what the next session should pick up.

## The architecture (decided 2026-09-27)

```
[ Robinhood quotes ] -> hourly bars (Coinbase fills in history)
              |
              v
[ The trend ladder ]   BTC and ETH: buy dips in dollar steps above the 50-day
              |        average, sell into strength, sell everything under it
              v
[ 14 risk rules ] -> proposal -> a human approves by id -> Claude places it via MCP
```

This replaced the System 1 / System 2 pipeline (indicators and Jev news labels,
an escalation trigger, Claude Sonnet 5 as a second opinion) at the owner's
request. The evidence that led here is in [`strategy.md`](./strategy.md#history).
In short: at Robinhood's ~1.9% round trip, a six-hour hold has to move more
than a typical six-hour move just to break even, and System 1's record matched
that. The dip ladder, filtered by the 50-day average, was the only variant
that beat holding per dollar on BTC and ETH across the 180, 365 and 730-day
windows.

## Where things stand

- **Built:** the trend ladder (`strategy/ladder.py`), live in `rhca run` and
  `rhca analyze` and replayed by `rhca backtest`, with a test holding the two
  to the same trades. Its state comes from recorded fills (`ledger.py`).
  `rhca status` shows each coin's position and realized P&L.
- **Backtest:** the trend exit, a `trend +50d` baseline, and rolling windows
  (`--roll-window`).
- **Not yet evidenced:** the trend exit. It was specified before any result,
  and the synthetic test shows it can churn when the price hovers near the
  average.

## Next

0. **The trend ladder failed its bar** on real BTC and ETH bars, 2026-09-27
   (`strategy.md`, "The trend exit, tested"). Approve none of its proposals.
   Its successor candidate, the **breakout**, scored 4 of 5 at two years and
   again at four (`strategy.md`, "Over four years"): it fails consistency.
   It has no live implementation.
0a. **The forward test** runs from 2026-09-28 (`strategy.md`, "The forward
   test"): the owner's split -- $100 bought the buy-low way and held, $400 in
   the breakout, no coin over 20% of the account -- on paper, inside
   `rhca run`. Keep `rhca run` running, and read the bar with `rhca shadow` on
   or after 2026-12-27. The per-coin limit (20%) and the watchlist (BTC, ETH,
   SOL, XRP) were set for it on 2026-09-28; what else a live split needs is in
   `strategy.md`, "What running it for real would take".
1. **Run the backtest on real bars** (runbook, "Backtesting"):
   `rhca backtest --days 180`, `--days 365` and `--days 730 --roll-window 90`.
   Hold `ladder (anchor) +50d exit` to the bar in
   [`strategy.md`](./strategy.md#validating-it). If the exit churns, compare
   it with `+50d`, and decide on `trend_exit` from that.
2. **Only then approve the first live proposal**, at the current caps: $5 to
   $20 steps, $35 at most per coin.
3. **Record every fill and cancellation.** The ladder's state is only as good
   as the audit log. Reconcile against `get_crypto_orders` weekly
   (runbook, "Reconciling against Robinhood").
4. **Measure it live on P&L**, from `rhca status`, over a month or more. The
   ladder trades a few times a month per coin, so the live evidence comes
   slowly; the backtest is the fast evidence.
5. **The dashboard's frontend** still shows System 1's panels (signals,
   System 2 verdicts, hit rates by stage). The payload now carries the
   ladder's `rule`, `step` and reason; the UI should show those, and the
   ladder's P&L, instead.

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
5. **The ladder's state comes from recorded fills**, never from memory, so it
   survives a restart and never sells a coin it did not buy.
6. **The Robinhood client stays read-only until Phase 2.**
7. **A failure is never an approval.**
