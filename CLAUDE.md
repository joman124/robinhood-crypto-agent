# CLAUDE.md

Operating rules for Claude when running this agent.

## ⚠️ This repo trades real money

`place_crypto_order` places a **real order against a real account**. There is
no paper-trading mode and no sandbox endpoint. Every rule below exists because
the failure it prevents would cost the user actual money.

## Your role

You are the only component that can **place an order**:

- **You** call the `RobinHood` MCP tools, including the order tools. No code in
  this repository can place or cancel an order.
- **The CLI** (`rhca`) holds the strategy, the risk limits, and the audit log.
  It gives you proposals and validated payloads.
- **`rhca run`** is a shadow-mode loop that runs on its own. It *reads* from
  Robinhood's Crypto API with a read-only client (quotes and pairs only; the
  Agentic account's balance and holdings come from `rhca ingest`). Each closed
  hour it runs the trend ladder (`docs/strategy.md`) on every watchlist coin
  (BTC, ETH, SOL, XRP), and logs what the ladder wants as proposals. It never
  orders.
- **The forward test** also runs inside `rhca run` (`config/shadow.yaml`): the
  split account -- $100 long-term, $400 in the breakout -- on paper. After
  each UTC daily close it writes a `shadow_day` record of its paper fills.
  `rhca shadow` reports it. A paper fill is **not a proposal**: it has no
  proposal id, and it is never a reason to place an order.
- **A human** approves a specific proposal by its id. Nothing else is approval
  — including the ladder wanting it, and including a proposal the dashboard
  shows as ready.

## Hard rules

1. **Never call `place_crypto_order` without an explicit human instruction
   naming the proposal id.** "That looks good", "yes", and "go ahead" are not
   approval — in a report listing four symbols they are ambiguous about which
   one. Ask which proposal is meant. `rhca approve` enforces this; do not route
   around it by constructing a payload yourself.

2. **Run `rhca approve` before every `place_crypto_order` call.** It re-checks
   the kill switch, the approval text, the price drift, and the remaining
   unfilled quantity, and hands back the exact payload to send. Send that
   payload verbatim — do not edit a field in passing.

3. **Preview before placing.** Call `preview_crypto_order` with the payload
   from `rhca plan-order` first, and show the user the estimated cost and fees.
   Skip it only if the user very explicitly asks you to.

4. **Re-fetch the quote immediately before approving.** Pass it to
   `rhca approve --quote`. Past `price_drift_tolerance_pct` the gate refuses
   and asks for a fresh proposal — that is correct, not an obstacle. Re-run
   `rhca analyze` instead of retrying with a wider tolerance.

5. **Never execute a risk-blocked proposal** unless the human uses the exact
   phrase `OVERRIDE RISK CHECK` in the same message as the approval. Then log
   it with `record-execution --override` so the audit trail distinguishes "the
   limits allowed this" from "a human overrode the limits".

6. **Always show the risk verdict before offering to execute.** Every rule's
   result, not just the failures. Never place an order silently.

7. **Respect the kill switch.** If `rhca status` reports it engaged — manually
   or auto-engaged by the daily loss cap — refuse every execution and say why.
   Do not delete `data/KILL_SWITCH` to get past it.

8. **Never change `execution_mode` to `auto` on your own initiative.**
   Unattended trading is the intended Phase 2, but promoting to it is a
   deliberate human decision with preconditions (`docs/autonomy.md`), not
   something to infer from a conversation going well. Today the mode is not
   implemented, so setting it only makes every proposal fail the risk check --
   it is not a shortcut to a working automation mode.

9. **Never edit `config/risk_limits.yaml`, `config/agent.yaml`,
   `config/strategy.yaml` or `config/shadow.yaml` as a side effect** of
   executing a trade or wanting a proposal to pass. A config change is its own
   deliberate act, initiated by the human. If a limit blocks a trade, report
   that — do not widen the limit. Editing `config/shadow.yaml` restarts the
   forward test.

10. **Never fabricate a tool response.** Quotes, balances, fills, order ids and
    P&L go into the audit log only from real MCP output, piped in as JSON, or
    from `rhca run`'s own API reads. Do not retype, summarize, or reconstruct a
    response from memory. If a tool call failed, say it failed.

11. **Spot crypto only.** No margin, no leverage, no derivatives, even if the
    MCP server exposes them, unless the user explicitly widens the scope.

12. **Record every execution**, success or failure, with `rhca record-execution`
    before moving on. The daily notional and daily loss caps are computed from
    the audit log, so an unrecorded fill silently raises the day's remaining
    budget.

13. **Keep the shadow run shadow.** Never add an order or cancel method to
    `robinhood.py`. Unattended execution is Phase 2: it goes behind
    `ApprovalGate`, with the preconditions in `docs/autonomy.md`, as a
    deliberate human decision.

14. **Never print or log a credential.** `rhca status` lists key *names* only.
    Keys live in the environment or in the gitignored `.env`; they never go
    into config files, the audit log, or the dashboard payload.

## Following an execution plan

Every ladder proposal is **`PROMPT`**: one tranche, one limit order at its
`target_price` (the ask for a buy, the bid for a sell). Submit it promptly.
Never re-price it to chase a fill, and never add size to it.

Older proposals from the retired System 1 may be **`STAGED`** (several
tranches, one limit order each). If one is ever approved, log every tranche
with its own `record-execution --tranche N`; `rhca approve` refuses a tranche
that would exceed the remaining unfilled quantity.

The ladder rebuilds its state from the fills you record. Record the
`place_crypto_order` response, and then the fill from `get_crypto_orders`
once it happens (or the cancellation, if it never fills). Until then it
treats the step as taken and will not propose it again.

## Tool-contract facts worth remembering

Run `rhca describe-tools` for the full list, and `rhca validate-order` to check
any payload offline. The traps:

- Order tools take **`rhs_account_number`** (numeric). `get_portfolio` takes
  **`account_number`** (alphanumeric). They are different fields.
- Exactly one of `quantity` or `dollar_amount`.
- `market` and `limit` accept only `time_in_force: "gtc"`. **`ioc` is never
  valid for crypto.** Stop types also accept `gfd`/`gfw`/`gfm`.
- `ref_id` is an idempotency key: one UUID per logical order, re-sent
  **verbatim** when retrying a transient failure. A new UUID means a new order.
- `get_crypto_quotes` returns symbols **unhyphenated** (`BTCUSD`);
  `get_currency_pairs` returns them hyphenated (`BTC-USD`).
- A `market_orders_only` pair rejects limit orders.
- A dollar-sized market buy can cost ~1% more than requested; a sell can return
  ~5% less. Say so when quoting an estimate.

## Typical session

```
rhca status                                   # mode, kill switch, today's usage
# call get_accounts, get_currency_pairs, get_portfolio, get_crypto_positions,
# get_crypto_quotes -- pipe each response into `rhca ingest <kind>`
rhca analyze -v                               # proposals + full risk verdicts
# show the user; they approve ONE proposal BY ID
rhca plan-order <id>                          # -> preview_crypto_order payload
# call preview_crypto_order, show the estimate
# re-fetch get_crypto_quotes into fresh.json
rhca approve <id> --approval "<their exact words>" --quote fresh.json
# call place_crypto_order with the payload it printed, verbatim
rhca record-execution <id> --tranche 0 -f response.json
```

When `rhca run` is running, its proposals are already in the audit log with a
`status` (`proposed` or `rejected_by_risk`; System 1's old records also carry
`declined_by_system2` and `not_escalated`). Act only on `proposed` ones a human
names by id, through the same `plan-order` → `approve` → `place_crypto_order`
→ `record-execution` steps. Check `rhca status` for whether the loop is alive
before trusting its quotes; it also shows each coin's ladder position and
realized P&L.

Once a day, record realized P&L so the daily loss cap is real:

```
# call get_realized_pnl
rhca record-pnl -f realized_pnl.json
```
