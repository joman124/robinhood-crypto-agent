# Architecture

## The split

```
Claude Code ──── MCP ────► Robinhood         the only order path
     │
     │ JSON in / payloads out
     ▼
rhca (this package) ── read-only API ──► Robinhood   quotes, pairs
                    ── HTTPS ──────────► RSS, Jev, Claude Sonnet 5
                                           (+ Crypto.com market data via MCP)
                    ── order? ─────────►  ✗   (no code path exists)
```

The package now reads from Robinhood (`rhca run`, `robinhood.py`), and calls
news feeds, Jev and Anthropic. It still cannot place or cancel an order: the
Robinhood client has only GET methods, and System 2's tools are read-only apart
from its own decision. So a bug in this repository still costs a bad proposal,
never a bad order.

The decision path is still testable offline. Every network client is injected
(`runner.Services`), and the test suite fakes each one and touches no network.

## The real-time loop (`rhca run`)

```
quotes (Robinhood) ─┐
RSS ─► Jev ─────────┼─► System 1: candles ─► 4 price signals + news ─► composite
                    │                                   │
                    │               size ─► plan ─► 17 risk rules ─► candidate
                    │                                   │
                    │             trigger: confidence, |score|, cooldown, cap
                    │                  no ─► logged (not_escalated)
                    │                  yes ─► System 2: Sonnet 5, propose/pass
                    │                                   │
                    └────────────► audit log ─► outcomes ─► dashboard
```

System 1 is deterministic and fast. The one model call inside it is Jev, which
labels text and never sees a price. System 2 runs only when System 1 is
already confident *and* the risk engine has already said yes. So Sonnet can
veto a trade, but it can never talk the system into one the rules refuse.

## The pipeline

```
ingest ──► price store ──► candles ──► signals ──► composite ──► sizing
                                          ▲                        │
                          news (Jev) ─────┘   proposal ◄── plan ◄── risk
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
| `risk.py` | 17 rules, all evaluated, each naming itself |
| `execution/orders.py` | Execution plans and validated order payloads |
| `execution/gate.py` | The approval gate — the one path to an order payload |
| `execution/kill_switch.py` | File-based, fail-safe stop |
| `audit.py` | Append-only log; the daily caps are computed from it |
| `robinhood.py` | Read-only, Ed25519-signed Crypto Trading API client — no order methods |
| `news.py` | RSS/Atom parsing and the append-only news store |
| `jev.py` | Jev (TypeSafe AI) labels a headline: asset, direction, impact |
| `trigger.py` | "Is confidence high?" — thresholds, cooldown, daily cap |
| `system2.py` | Claude Sonnet 5: propose or pass, read-only tools, plus an allowlisted Crypto.com market-data MCP connector |
| `runner.py` | `rhca run`: cadences, dedupe, heartbeat, per-task failure isolation |
| `bootstrap.py` | Coinbase candles, so a fresh checkout has history at once |
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
