# Risk controls

Five layers. They are deliberately redundant — each assumes the others might
have been misconfigured.

## 1. Hard code ceilings

`config.py` defines `ABSOLUTE_CEILINGS`. Any config value above its ceiling is
a **load error**, not a clamp:

```
max_notional_per_trade_usd=5000 exceeds the hard ceiling of 2500 set in
config.py. Raising a risk ceiling is a deliberate code change, not a config edit.
```

| Limit | Ceiling | Shipped default |
|---|---|---|
| `max_notional_per_trade_usd` | 2500 | 250 |
| `max_daily_notional_usd` | 10000 | 1000 |
| `max_daily_loss_usd` | 1000 | 200 |
| `max_position_pct_of_portfolio` | 25 | 10 |
| `max_open_positions` | 8 | 5 |
| `max_spread_pct` | 2.5 | 0.75 |
| `price_drift_tolerance_pct` | 1.5 | 0.5 |
| `max_quote_age_seconds` | 300 | 90 |

Config can only make the agent **more** conservative. Widening a limit past its
ceiling requires editing code, which shows up in a diff and gets reviewed.

There are floors too, where "too small" is the unsafe direction
(`min_signal_confidence`, `min_notional_per_trade_usd`), and consistency checks
— a per-trade cap above the daily cap is rejected, since no single trade could
ever pass.

## 2. The risk engine

Sixteen rules run on **every** proposal, and **all of them run**. Nothing
short-circuits on the first failure, so the report shows every reason a trade
was blocked — fixing one and rediscovering the next is how a limit gets
whittled away one edit at a time.

| Rule | Blocks when |
|---|---|
| `execution_mode` | mode is not `propose_only` |
| `kill_switch` | the switch is engaged |
| `watchlist` | the symbol is not on the allowlist |
| `pair_tradable` | untradable, or globally halted (regional halt → warning) |
| `order_type_supported` | a limit order on a `market_orders_only` pair |
| `quote_freshness` | the reference quote is older than the cap |
| `spread` | bid/ask spread exceeds `max_spread_pct` |
| `signal_confidence` | composite confidence below the floor |
| `signal_strength` | composite \|score\| below the floor |
| `sizing` | the sizing model declined, with its reason |
| `per_trade_notional` | **worst case after the collar** exceeds the cap |
| `daily_notional` | today's executions + this trade exceed the daily cap |
| `daily_loss` | today's realized loss reaches the cap |
| `open_positions` | this would open one position too many |
| `concentration` | the position would exceed its portfolio share |
| `sell_coverage` | selling more than is held |

Two details worth calling out:

**The collar.** A dollar-sized market buy can cost ~1% more than requested and
a sell can return ~5% less. `per_trade_notional` checks the worst case after
that collar, not the nominal — so a cap that passes still holds if the price
moves on the way in.

**Warnings vs blocks.** A missing portfolio value makes `concentration`
*unenforceable*, so it reports a non-blocking warning saying exactly that
rather than silently passing. A regional trading halt warns rather than blocks,
because it may be one-sided.

## 3. The approval gate

The one path from a proposal to an order payload. It requires:

- **An approval naming the proposal id**, matched on a word boundary. "That
  looks good" is refused with a message saying so. Another proposal's id does
  not authorize this one.
- **`OVERRIDE RISK CHECK`**, verbatim and in the same message, to execute a
  risk-blocked proposal — logged as an override. Using the phrase on a proposal
  that *passed* is also refused, so it cannot become a habitual incantation
  attached to every approval.
- **A fresh quote within `price_drift_tolerance_pct`** of the proposal's
  reference price. Past that, the gate refuses and asks for a fresh proposal
  rather than filling against a stale quote.
- **Remaining unfilled quantity.** A staged plan fills over tranches; the gate
  subtracts what already filled and refuses a tranche that would exceed the
  remainder, so re-approving cannot silently double a position.

## 4. The kill switch

The switch is the **presence of a file** (`data/KILL_SWITCH`), not a flag in a
config object or a variable in a process:

- It can be engaged from outside the agent — `touch data/KILL_SWITCH` from any
  shell — with no session to find and no process to signal.
- It survives a crash and a restart. The moment you most want a kill switch is
  right after something went wrong.
- **It fails safe**: if the file is present but unreadable, the switch reads as
  *engaged*. Unknown state must block, never permit.
- Re-engaging keeps the original reason and timestamp, so a later manual engage
  does not erase why trading first stopped.

It auto-engages when recorded realized loss reaches `max_daily_loss_usd`.

## 5. The audit log

Append-only JSONL, and **load-bearing**: `daily_activity()` is what the daily
notional and daily loss caps are computed from, so the caps survive restarts
and separate sessions on the same day.

- Proposals the risk engine **blocked** are logged too. That record of what the
  agent wanted to do and was stopped from doing is the evidence the controls
  work.
- Rejected, failed and voided orders do **not** consume the daily notional
  budget — they moved no money.
- Realized P&L for a day is **superseded**, not summed: Robinhood reports a
  running total, so summing would double-count.
- A hand-edited log raises `AuditError` on read rather than being silently
  reinterpreted.

## What is deliberately *not* protected against

- **A compromised or mistaken Claude session.** These controls constrain what
  the CLI will authorize. Claude could call `place_crypto_order` directly with
  a payload it made up. The mitigations are procedural — `CLAUDE.md` rule 1,
  and a human reading the risk verdict before approving.
- **Market risk.** Nothing here predicts price. Every limit bounds *size and
  frequency*, not outcome.
- **Unrecorded fills.** If an execution is never recorded, the day's remaining
  budget stays too high. Recording every execution is rule 12 in `CLAUDE.md`.
