# Roadmap and handoff

Where the project stands, and what the next session should pick up.

## Where things stand

- The agent is on `main` and deployed: proposals, 16 risk rules, the approval
  gate, the audit log, outcome scoring, and a Vercel dashboard for sign-off.
- **Not yet exercised against a funded account.** No order has been placed,
  previewed or cancelled by this codebase. [`runbook.md`](./runbook.md) has the
  first-run checklist — start by confirming the kill switch actually stops a
  trade before trusting anything else.
- **No real track record yet.** Any hit rate shown so far came from synthetic
  data used to exercise the pipeline. The real number starts accumulating once
  quotes are ingested on a schedule — which is goal 1.

## Goal 1 — run the loop hourly

**The trap: an hourly cron running only `rhca dashboard-sync` does nothing useful.**

`dashboard-sync` reads local state and pushes it. It does not fetch quotes and
does not analyze. Run alone on a timer it would re-push the same proposals
forever, and — because outcomes resolve by comparing a proposal against the
bars that came *after* it — every pending proposal would stay pending. The hit
rate would never move.

The hourly job has to be the whole loop:

```
get_crypto_quotes (MCP)  ->  rhca ingest quotes   # new bars; resolves outcomes
                             rhca analyze         # new proposals
                             rhca dashboard-sync  # push up, pull decisions back
```

Only the first step is the problem: fetching quotes goes through the RobinHood
MCP server, which means it needs Claude. Plain `crontab` cannot do it, because
`cron` has no MCP connection and this package deliberately has no Robinhood
credentials of its own.

**Recommended: a scheduled Claude session** (a Routine on an hourly cron) that
runs the four steps above. It keeps the architecture intact — Claude stays the
only thing that touches Robinhood — and it is the same mechanism Phase 2 would
use, so building it now is a step toward autonomy rather than a detour.

Worth deciding when building it:

- **Ingest more often than you analyze.** Bar quality is driven by sampling
  density — roughly four quotes per bar interval is where confidence stops
  being penalised (see [`data-constraints.md`](./data-constraints.md)). At
  60-minute bars that is a quote every ~15 minutes, with `analyze` hourly.
- **The job must not place orders.** It proposes and syncs; execution still
  waits for a human decision. That boundary is the whole design.
- **Failures must be visible.** A scheduled job that silently stops looks
  exactly like a quiet market. Some heartbeat — even just noticing that
  `rhca status` reports a stale last-observation age — is worth having before
  relying on it. This is the same dead-man's-switch problem Phase 2 needs
  solved properly ([`autonomy.md`](./autonomy.md)).

## Goal 2 — improve the dashboard UX

Observations from building and using it. Roughly in order of value:

**Stops wasted clicks**
- **Show whether a proposal is still live.** A proposal drifts out of
  acceptability as the price moves; past `price_drift_tolerance_pct` the
  approval gate refuses it. The page currently offers Accept on proposals the
  agent would then reject. Showing an age or a "likely stale" marker — and
  greying out the button past the tolerance — would stop the dead-end click.
- **Relative timestamps** ("2h ago") instead of absolute ones. The question
  being asked of that column is *is this still fresh*, not *what time was it*.

**Makes the numbers legible**
- **Explain the signal column.** `0.33 · 100%` is opaque. It is composite score
  and confidence; it should say so, on hover at least.
- **Surface why a proposal exists.** The per-signal breakdown and rationale are
  already in the audit log and already pushed in the payload's `outcome`, but
  the page never shows them. A detail view or expandable row would make a
  proposal reviewable rather than just approvable.
- **Collapse the accuracy panels when they are single-row.** With one regime
  and one pair, "by regime" and "by pair" are two identical bars and add
  nothing. Render them only when there are at least two groups.
- **A hit-rate trend.** The single headline number hides whether the agent is
  getting better or worse. A sparkline of rolling hit rate over time answers
  the question the number is standing in for.

**Scale and ergonomics**
- **Paginate or virtualise the table.** It renders every proposal ever; the
  filter helps, but "All" will eventually be thousands of rows.
- **Filter by pair and by outcome**, not just decision state.
- **A card layout on mobile.** The table scrolls horizontally on a phone, which
  is workable but not good.

## Carried over

- **Phase 2, unattended execution.** Gated, with written promotion criteria, in
  [`autonomy.md`](./autonomy.md). Goal 1 is a prerequisite: without a scheduled
  loop there is no evidence to promote on.
- **joman124/robinhood-crypto-agent#1** is still open and superseded — it
  targets an MCP endpoint that does not exist and assumes crypto candles are
  available. Closing it is the owner's call.

## What not to erode

Each of these was load-bearing enough to be worth writing down:

1. **The dashboard never places orders and holds no Robinhood credentials.**
   Accepting records a decision; the agent replays it through the full gate.
2. **`OVERRIDE RISK CHECK` stays typed-only.** Guarded twice — redacted from
   decision notes on both sides, and refused by the gate for any approval that
   did not come from a typed instruction.
3. **Config can only tighten.** Raising a ceiling is a code change.
4. **An unknown hit rate reads "unknown", never 0%**, and an unresolved
   proposal is never scored as a loss.
5. **Proposals are scored whether or not they were accepted**, so the record is
   not flattered by only counting the ones a human liked.
