# Roadmap and handoff

Where the project stands, and what the next session should pick up.

## The architecture (decided 2026-09-21)

```
[ Real-time crypto data / news ]      Robinhood quotes, RSS; Crypto.com market data for System 2
              |
              v
[ Jev + System 1 ]                    Jev (TypeSafe AI) labels each headline;
              |                       indicators + news signal + 16 risk rules
      Is confidence high?             trigger: thresholds, cooldown, daily cap
       |-- no  -> logged as not_escalated (and still scored)
       '-- yes -> [ Claude Sonnet 5 ] System 2: propose or pass, with read-only tools
                          |
                          v
              [ Robinhood Crypto API ]  shadow mode: a logged proposal, not an order
```

This replaced the previous Goal 1, an hourly scheduled ingest-analyze-sync
loop. `rhca run` does the same job continuously, and it builds the track record
that loop was meant to build.

## Where things stand

- **Built:** `rhca run`, in shadow mode. It polls Robinhood's Crypto Trading API
  with a read-only, Ed25519-signed client (`robinhood.py`), which has no order
  method. It fetches five free RSS feeds. (X was dropped on 2026-09-21: it
  bills per post read.) Jev labels each headline with asset, direction and impact. The
  labels feed a fifth, *optional* signal (`NewsSignal`), which drops out of the
  blend entirely when there is no recent news. Candidates that pass every risk
  rule and the trigger go to Claude Sonnet 5 (`system2.py`), which answers
  propose or pass. Sonnet can also read a second venue's market data (ticker,
  order book, candles, trades) from Crypto.com's free public MCP server, through
  the Anthropic API's MCP connector. That toolset is an allowlist of its
  read-only tools. Every candidate is logged with its status and scored.
  `rhca accuracy` groups hit rates by status.
- **Also built:** `rhca bootstrap-history` imports free Coinbase bars, so the
  indicators work from the first minute instead of after 30 hours. The price
  store reads incrementally. Dashboard sync no longer re-notes the same
  decision on every sync.
- **Not yet exercised with real keys.** No key has been used yet. The Robinhood
  REST response shapes come from the docs and have not been confirmed against
  a live account. The first `rhca run --once` is that check; see the runbook.
- **Still no live order path anywhere in the package.** Orders still go only
  through Claude Code's MCP tools, behind a human approval by proposal id.

## Next: the shadow run

1. **Keys.** Robinhood Crypto API, TypeSafe (Jev), and Anthropic. The Crypto.com connector needs none.
   The owner creates them. `.env.example` lists them, and
   `docs/runbook.md` covers setup.
2. **First `rhca run --once`.** Confirm quotes, pairs, holdings and buying
   power parse. Then replace the invented REST fixtures in
   `tests/unit/test_robinhood_client.py` with trimmed live captures.
3. **Let it run for days, not hours.** Outcomes resolve six bars after each
   candidate. The questions only have answers once there are dozens of
   resolved rows per status:
   - Does System 2 add value? Compare hit rate for `proposed` against
     `declined_by_system2`.
   - Is the trigger in the right place? Compare escalated candidates against
     `not_escalated`.
   - Does news help? Compare candidates with a `news` signal against those
     without. Not grouped yet; this is the next small report worth adding.
4. **Tune from evidence, not feel.** The trigger thresholds and news weight
   live in `config/pipeline.yaml` and `config/strategy.yaml`. Jev's
   confidence measures how sure it is of a *label*, not whether the trade
   wins, so its threshold is only as good as the outcomes behind it.

## Done: dashboard UX (2026-09-23)

Every item on the old list shipped: the pipeline's fields and a stage filter,
`by_status` accuracy, Accept greyed out past `price_drift_tolerance_pct`,
relative timestamps, the signal column explained, per-signal rationale,
single-row accuracy panels collapsed, a hit-rate trend, pagination,
pair/outcome filters and a mobile card layout. The payload (version 2) also
gained the kill switch, the loop heartbeat, the last mark per pair, per-rule
risk verdicts and an execution summary. See [`dashboard/README.md`](../dashboard/README.md).

What stays out, deliberately: System 2's written rationale (Sonnet reads the
holdings and may quote them), the text of risk messages that quote the account,
and the loop's last error text. Read those locally with `rhca audit` and
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
3. **Config can only tighten.** Raising a ceiling is a code change. The
   pipeline's spend settings have ceilings too.
4. **An unknown hit rate reads "unknown", never 0%**, and an unresolved
   proposal is never scored as a loss.
5. **Every candidate is scored, whatever its status**, so the record is not
   flattered by counting only what a stage let through.
6. **The Robinhood client stays read-only until Phase 2.** System 2's tools
   stay read-only too. Its only write is `submit_decision`, and that writes
   a log row.
7. **A failure is never an approval.** An API error, a refusal, a timeout, a
   malformed answer or running out of turns all record as not approved.
8. **Only a pre-approved candidate reaches System 2.** A candidate the risk
   engine blocked is never escalated, so no headline can talk Sonnet into a
   trade the rules would refuse.
