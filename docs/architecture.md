# Architecture

## The split

```
Claude Code ──── MCP ────► Robinhood         the only order path
     │
     │ JSON in / payloads out
     ▼
rhca (this package) ── read-only API ──► Robinhood   quotes, pairs
                    ── HTTPS ──────────► Coinbase    public candles (history)
                    ── order? ─────────►  ✗   (no code path exists)
```

The package reads from Robinhood (`rhca run`, `robinhood.py`) and fetches
Coinbase's public candles for history. It cannot place or cancel an order: the
Robinhood client has only GET methods. So a bug in this repository costs a bad
proposal, never a bad order.

The decision path is testable offline. Every network client is injected
(`runner.Services`), and the test suite fakes each one and touches no network.

## The real-time loop (`rhca run`)

```
quotes (Robinhood) ─► price store ─► closed hourly bars ─┐
                                                          ├─► trend ladder ─► size ─► plan
recorded fills (audit log) ─► ledger: held, anchor, steps ┘                            │
                                                                          14 risk rules
                                                                                       │
                                               dashboard ◄── audit log ◄── proposal ◄──┘
```

The rule (`strategy/ladder.py`) is a pure function of the last closed bar, its
trend average, and the ladder's state. The state is not kept in memory: the
ledger rebuilds it from the audit log on every pass, so a restart changes
nothing, and the ladder only ever sells what its own recorded fills bought.
`rhca backtest` drives the same function over history.

## The pipeline

```
ingest ──► price store ──► candles ──┐
                                     ├─► ladder.decide ──► sizing ──► plan ──► risk
audit log ──► ledger ────────────────┘                                          │
                                                   approval gate ◄── proposal ◄─┘
                                                         │
                                                   order payload
```

Each stage may decline, and a decline carries a reason. A symbol always ends
with either a proposal or an explanation — an empty result list with no
explanation would hide the difference between "nothing looks good" and "this
symbol has no data". With no order to make, the explanation says where the
next step would buy or sell.

## Modules

| Module | Responsibility |
|---|---|
| `mcp/contract.py` | The RobinHood order contract as checkable rules |
| `mcp/parse.py` | Response parsers, written against live payload shapes |
| `store/prices.py` | Append-only observations; bars derived on read |
| `store/state.py` | Cached account/market snapshot, written atomically |
| `strategy/ladder.py` | The trend ladder: one pure rule, shared by the loop and the backtest |
| `ledger.py` | The ladder's holdings, cycle and P&L, rebuilt from recorded fills |
| `agent.py` | Bars + ledger → the rule → sized, planned, risk-checked proposals |
| `sizing.py` | A step's dollars → a quantity the pair accepts, snapped down |
| `risk.py` | 14 rules, all evaluated, each naming itself |
| `execution/orders.py` | One-order plans and validated order payloads |
| `execution/gate.py` | The approval gate — the one path to an order payload |
| `execution/kill_switch.py` | File-based, fail-safe stop |
| `audit.py` | Append-only log; the daily caps are computed from it |
| `robinhood.py` | Read-only, Ed25519-signed Crypto Trading API client — no order methods |
| `runner.py` | `rhca run`: cadences, once-per-bar dedupe, heartbeat, per-task failure isolation |
| `bootstrap.py` | Coinbase candles: the 52 days the trend average needs, at once |
| `backtest.py` | `rhca backtest`: the ladder against its baselines, whole and in rolling windows |
| `outcomes.py` | The retired System 1's six-hour hit rate, kept for its records |
| `net.py` | The one HTTP helper: timeouts, size cap, error wording |

## The contract layer

`mcp/contract.py` encodes the published RobinHood MCP order schema as
assertions. It exists because the dangerous failure is not a crash — it is a
*plausible-looking but invalid* payload that reaches a live account.

Rules enforced offline, before Claude calls anything:

- `rhs_account_number` must be **numeric**. The alphanumeric `account_number`
  that `get_portfolio` takes is rejected with a message saying exactly that —
  this is the single easiest field to mix up.
- Exactly one of `quantity` / `dollar_amount`.
- `limit_price` required for `limit`/`stop_limit` and **rejected** on
  `market`/`stop_loss`; `stop_price` required for the stop types.
- `market` and `limit` accept only `gtc`. **`ioc` is never valid for crypto.**
- `ref_id` must be a UUID.
- `tax_lots`: sell-only, quantity-only, ≤50 lots, summing exactly to the order
  quantity, ≤8 decimal places each.
- Unknown argument names are rejected rather than silently passed through.

`rhca validate-order` exposes this as a standalone check for any payload.

## Decimals, not floats

Money and quantities are `Decimal` throughout, and no price is ever converted
to `float`. The order API takes decimal strings, and a float round-trip is how
`0.3` becomes `0.30000000000000004` in a quantity field. `format_decimal`
additionally guarantees no exponent notation — `str(Decimal("1E-8"))` is
`"1E-8"`, which the API will not accept.

The trend average is a `Decimal` too, so the rule compares like with like and
the backtest and the live loop make identical decisions.

## Why bars exclude the partial current bar

A decision on a bar five minutes into its hour changes under its own feet. A
rule that flips between two runs minutes apart is worse than no rule, and the
backtest decides on closed bars, so the forming bar is excluded unless
`include_partial` is set.

## Why the audit log is load-bearing

`daily_activity()` is not a reporting convenience — it is what the daily
notional and daily loss caps are computed from. That means the caps survive a
restart, a new shell, and a crashed session, because they derive from durable
state rather than anything held in memory. It also means an unrecorded fill
silently raises the day's remaining budget, which is why recording every
execution is rule 12 in `CLAUDE.md`.
