# CLAUDE.md

Operating rules for Claude when running this agent.

## ⚠️ This repo trades real money

Accepting a proposal on the dashboard places a **real order against a real
account**, and so does `place_crypto_order`. There is no paper-trading mode and
no sandbox endpoint. Every rule below exists because the failure it prevents
would cost the user actual money.

## Your role

Since 2026-10-20 the split trades the owner's **main crypto account**, and the
only way its orders are placed is the owner's Accept on the dashboard:

- **The owner** accepts one proposal by typing its id on the dashboard.
- **`rhca run`** pulls that accept within a minute and its order desk
  (`execution/desk.py`) runs it through `ApprovalGate` and places that one
  limit order through the Robinhood Crypto API, on the API key's account --
  the main one. It follows the order to its fill and records every step in the
  audit log. It never places an order without that accept.
- **The CLI** (`rhca`) holds the strategy, the risk limits, and the audit log.
- **You** read, explain and build. Do **not** place split orders with the
  `RobinHood` MCP order tools: they reach the **Agentic** account, which the
  split no longer trades, so its ledger, caps and holdings would all be wrong.
- **`rhca run`** runs on its own. It reads quotes, pairs, buying power and
  holdings from the API key's account. After each UTC daily close it runs
  **the split** (`docs/strategy.md`, "The split") on every watchlist coin (BTC,
  ETH, SOL, XRP) -- long-term, bought the buy-low way and held, and the
  breakout -- and logs what it wants as proposals, afresh each hour.
- **The forward test** also runs inside `rhca run` (`config/shadow.yaml`): the
  same split on paper. After each UTC daily close it writes a `shadow_day`
  record of its paper fills. `rhca shadow` reports it. A paper fill is **not a
  proposal**: it has no proposal id, and it is never a reason to place an
  order. Each paper fill also carries a risk verdict, and `rhca shadow` prices
  the passing ones at Robinhood's bid and ask: what approving them *would*
  have made. A passing verdict on a paper fill is not approval either. The
  dashboard's forward-test card shows the same paper trades: none has a
  proposal id, so none can be approved from it.
- **A human** approves a specific proposal by its id, on the dashboard. Nothing
  else is approval — including the split wanting it, and including a proposal
  the dashboard shows as ready.

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

13. **One order path, behind the owner's accept.** `robinhood.py` has exactly
    one order method, `place_order`, and only the order desk calls it, after a
    dashboard accept naming that proposal clears `ApprovalGate` (the owner's
    decision, 2026-10-20). Never add a cancel method or another caller, and
    never let the desk act without an accept: unattended execution is Phase 2,
    with the preconditions in `docs/autonomy.md`, as a deliberate human
    decision.

14. **Never print or log a credential.** `rhca status` lists key *names* only.
    Keys live in the environment or in the gitignored `.env`; they never go
    into config files, the audit log, or the dashboard payload.

## Following an execution plan

Every split proposal is **`PROMPT`**: one tranche, one limit order at its
`target_price` (the ask for a buy, the bid for a sell). Submit it promptly:
the rule decided on the last daily close, and its entry is meant to fill near
it. Never re-price it to chase a fill, and never add size to it. If the price
has moved past the drift tolerance, run `rhca analyze` for a fresh proposal
priced at the current quote -- the day's decision stands until the next close.

A proposal's `sleeve` says whose money it is: a **short-term** breakout entry
or stop, or a **long-term** tranche. Long-term tranches are never sold; a stop
sells only what the short-term sleeve holds of that coin.

Older proposals from the retired System 1 may be **`STAGED`** (several
tranches, one limit order each). If one is ever approved, log every tranche
with its own `record-execution --tranche N`; `rhca approve` refuses a tranche
that would exceed the remaining unfilled quantity.

The split rebuilds each sleeve's cash and holdings from the fills the order
desk records: `submitting` before it sends, Robinhood's answer, then each fill
(as a delta) and the order's end as `rhca run` reads it back every minute.
Until the order ends it treats it as working: it will not propose it again,
and an open buy's dollars stay out of the sleeve's cash. A limit order that
never fills stays open (gtc) until it is canceled in the Robinhood app; the
desk then records the cancellation.

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
rhca status          # loop alive, kill switch, today's usage, each sleeve
rhca analyze -v      # what the split wants now, with every risk verdict
# the owner accepts ONE proposal BY ID on the dashboard; rhca run places it
# within a minute and pushes "Order placed", then "Order filled" (or why not)
rhca status          # the order and the sleeves, as the audit log has them
```

`rhca run`'s proposals are in the audit log with a `status` (`proposed` or
`rejected_by_risk`; System 1's old records also carry `declined_by_system2`
and `not_escalated`). Only a `proposed` one can be accepted. Check `rhca status`
for whether the loop is alive before trusting its quotes. The
`plan-order` → `approve` → `place_crypto_order` → `record-execution` steps
remain for the MCP tools, which reach the Agentic account -- not for the split.

Once a day, record realized P&L so the daily loss cap is real:

```
# call get_realized_pnl for the main crypto account (the one the split trades)
rhca record-pnl -f realized_pnl.json
```
