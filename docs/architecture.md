# Architecture

## Principle: Python never talks to Robinhood

The `robinhood-trading` MCP server is the only trusted path to account state,
live quotes, and order placement/cancellation, and it is only ever called by
Claude inside an interactive Claude Code session — never by a standalone
script holding credentials.

| Concern | Owner |
|---|---|
| Account state, live quotes, order placement/cancellation | MCP `robinhood-trading` server, called by Claude |
| Historical data for backtesting, external signals | Python, via free/public APIs — separate from and never a substitute for the MCP execution path |
| Signal generation, regime detection, proposal construction, risk checks, sizing | Python CLI (`analyze`) |
| Strategy validation | Python CLI (`backtest`) |
| Approve/execute decision | A human, via explicit instruction to Claude naming a proposal ID |
| Order submission | Claude, calling the MCP order tool with exactly the approved proposal's fields |
| Recording what happened | Python CLI (`log-execution`), fed the MCP response by Claude |

## Flow (Phase 1: analyze-and-propose-only)

1. A human, in a Claude Code session in this repo, asks Claude to run the
   agent.
2. Claude fetches current market data for the watchlist via the MCP
   market-data tool and feeds it to `robinhood-crypto-agent analyze`.
3. `analyze` runs regime detection, the composite multi-signal strategy, and
   the risk engine. Every candidate — including risk-rejected ones — is
   written to the audit log with its full reasoning.
4. The human reviews the proposal report and tells Claude, explicitly and by
   ID, which proposal(s) to execute (or none).
5. Claude re-confirms the current price via MCP (guards against a stale
   proposal), then calls the MCP order-submission tool with exactly that
   proposal's symbol/side/quantity/order type.
6. Claude immediately runs `log-execution` with the raw MCP response, which
   appends an authoritative `ExecutionRecord` to the audit log.

## The automation extension point

All execution goes through a single call site:

```python
get_execution_adapter(config).submit_order(proposal)
```

In Phase 1, the only implementation (`HumanApprovalRequiredAdapter`) always
raises `ExecutionNotAutomatedError` — it exists purely so there is one call
site and one exception type to reason about. Graduating to automated
execution later means:

1. Implementing a new `ExecutionAdapter` (e.g. one backed by an MCP client
   invoked directly from Python).
2. Extending `get_execution_adapter`'s factory to return it when
   `execution_mode == AUTO`.
3. Flipping `execution_mode` in config — a deliberate, human-only action (see
   `CLAUDE.md`).

Strategy, risk, and audit logic are unchanged by this transition.

## Why not have Python call Robinhood/MCP directly?

Technically possible via an embedded MCP client, but it would duplicate the
trusted execution path, likely can't reuse whatever session/auth Claude
Code's own connection to the HTTP MCP transport establishes, and would
quietly turn this into a standalone bot — contradicting "operated through
Claude Code." Left as a documented future option behind the same
`ExecutionAdapter` interface, not built in Phase 1.
