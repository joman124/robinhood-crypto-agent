---
name: rhca-status
description: >-
  Report the agent's health from `rhca status`: whether the `rhca run` loop is
  alive, the kill switch, today's usage against the daily caps, each sleeve of
  the split, and any order still working. Use when the owner says "status",
  "is it running?", "is the loop up?", "is the kill switch on?", "how much can
  it still trade today?", "what does it hold?" or "any open orders?", and at
  the start of every session that operates this agent.
---

# rhca status

Every session starts here, and checks here again after an order. `rhca status`
reads the config, the audit log, the loop's heartbeat and the cached market
state. It calls no broker and changes nothing. On the owner's Windows PC it
runs from the repo root as `.venv\Scripts\rhca status` (`docs/runbook.md`,
"Shadow run", step 3).

```
rhca status                              # the whole report
rhca kill-switch status                  # the switch alone
rhca audit --kind execution --limit 20   # the latest order records: what is still working
rhca audit --proposal-id <id>            # one order's proposal, fills and total filled
```

## What it reads

| `rhca status` line | What it means |
|---|---|
| `execution mode` | Always `propose_only`. Anything else is a finding (CLAUDE.md rule 8). |
| `kill switch:` | `released`, or `ENGAGED (<reason>) since <time>`. `daily_loss_cap` means the loss cap tripped it. |
| `<date>: N execution(s), ...` | Today, UTC, from the audit log. |
| `daily notional remaining`, `daily loss` | Today against `max_daily_notional_usd` and `max_daily_loss_usd`. |
| `<section>: updated N minutes ago` | The cached quotes, pairs, positions, portfolio and buying power. |
| `pipeline` | `RUNNING` or `NOT RUNNING`. A heartbeat over 3 minutes old is not running. |
| `robinhood` | The account the API key reads: the one the split trades. |
| `split`, `long-term`, `short-term` | Each sleeve's cash, realized P&L, and each coin it holds. |
| `forward test` | The paper account. The `rhca-forward-test` skill reads it. |

The `account` line is the `rhs_account_number` of the MCP path (the Agentic
account). The split does not use it, so `NOT CONFIGURED` there is not a fault.

## Steps

1. Run `rhca status`. If it exits non-zero, show the error exactly as printed
   and stop: a config that does not load is the finding.
2. **Kill switch.** Quote its line. If it reads `ENGAGED`, say so first, with
   the reason and the time, and that nothing can be executed until the owner
   releases it (rule 7). `daily_loss_cap` is the control working (runbook,
   "Incidents").
3. **The loop.** Quote the `pipeline` line and any `last error` line exactly.
   `NOT RUNNING` or `never run` means no proposals are logged, no accept is
   placed, and no forward-test close is recorded. Point the owner at the
   runbook ("Shadow run", step 3) to start it.
4. **Today's usage.** Quote the activity line and both cap lines. Quote
   `DAILY LOSS CAP REACHED` if it appears. The `daily loss` figure counts only
   P&L recorded with `rhca record-pnl`. If none was recorded today, say the
   loss cap reads $0 for that reason (CLAUDE.md, "Typical session").
5. **The account.** Quote the `robinhood` line. `DOES NOT MATCH` or
   `not pinned` goes to the owner as a finding.
6. **Each sleeve.** Quote the `split` line, both sleeve lines, and every coin
   line under them.
7. **Open orders.** A coin line with `a buy is open` or `a sell is open` has an
   order working. Run `rhca audit --kind execution --limit 20` and take each
   `proposal_id`'s last record: a `state` other than `filled`, `canceled`,
   `rejected`, `failed` or `voided` is still working. A gtc limit that never
   fills stays open until the owner cancels it in the Robinhood app; the order
   desk then records the cancellation.
8. **Freshness.** `quotes` older than `max_quote_age_seconds` blocks every
   proposal by `quote_freshness`. A running loop refreshes them; if the loop
   is `NOT RUNNING`, that is the cause.
9. Report in this order: kill switch, loop, today's usage, account, sleeves,
   open orders. Then each finding, one line each.

## Rules

- Repeat each status line exactly as printed. Never round, re-total or
  paraphrase a dollar amount, a count or an age.
- Never fill a gap from memory. If a command fails, say it failed (rule 10).
- A `proposed` row in the log is still only a proposal. A status report never
  implies that anything should be accepted.

## What it never does

- Releases the kill switch or deletes `data/KILL_SWITCH`, or suggests
  releasing it to make back a loss.
- Starts, stops or restarts `rhca run`. The loop runs the order desk, so where
  and when it runs is the owner's call.
- Edits `config/*.yaml` or `.env`, or changes `execution_mode` (rules 8, 9).
- Prints a credential. `keys set` lists names only (rule 14).
- Places, previews or cancels an order, through MCP or any other way.
