# Architecture

## The split

```
Claude Code ──── MCP ────► Robinhood
     │
     │ JSON in / payloads out
     ▼
rhca (this package) ──── no network at all ────►  ✗
```

The Python package has no HTTP client, no credentials, and no way to reach
Robinhood. It computes decisions and validates payloads; Claude calls the
tools. This is what makes the whole decision path — strategy, sizing, risk,
audit — testable offline, with no account and no mocking of a trading API.
The 245-test suite touches no network.

## The pipeline

```
ingest ──► price store ──► candles ──► signals ──► composite ──► sizing
                                                                   │
                                            proposal ◄── plan ◄── risk
                                                │
                                        approval gate ──► order payload
```

Each stage may decline, and a decline carries a reason. A symbol always ends
with either a proposal or an explanation — an empty result list with no
explanation would hide the difference between "nothing looks good" and "three
of five symbols have no data".

## Modules

| Module | Responsibility |
|---|---|
| `mcp/contract.py` | The RobinHood order contract as checkable rules |
| `mcp/parse.py` | Response parsers, written against live payload shapes |
| `store/prices.py` | Append-only observations; bars derived on read |
| `store/state.py` | Cached account/market snapshot, written atomically |
| `indicators.py` | Aligned indicator series, Wilder smoothing where it applies |
| `strategy/` | Four signal sources, regime detection, confidence-weighted blend |
| `sizing.py` | Conviction × volatility scaling × caps |
| `risk.py` | 16 rules, all evaluated, each naming itself |
| `execution/orders.py` | Execution plans and validated order payloads |
| `execution/gate.py` | The approval gate — the one path to an order payload |
| `execution/kill_switch.py` | File-based, fail-safe stop |
| `audit.py` | Append-only log; the daily caps are computed from it |

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

Indicators *are* floats: they are statistics, not monetary amounts, and never
flow back into an order field.

## Why bars exclude the partial current bar

An indicator computed on a bar five minutes into its hour changes under its own
feet. A signal that flips between two runs minutes apart is worse than no
signal, so the forming bar is excluded unless `include_partial` is set.

## Why the audit log is load-bearing

`daily_activity()` is not a reporting convenience — it is what the daily
notional and daily loss caps are computed from. That means the caps survive a
restart, a new shell, and a crashed session, because they derive from durable
state rather than anything held in memory. It also means an unrecorded fill
silently raises the day's remaining budget, which is why recording every
execution is rule 12 in `CLAUDE.md`.
