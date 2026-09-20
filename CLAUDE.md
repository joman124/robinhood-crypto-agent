# CLAUDE.md

## ⚠️ This repo trades real money

There is **no paper-trading layer**. Every order this agent submits is a real,
live order against the user's Robinhood account, placed via the
`robinhood-trading` MCP server. Treat every execution step with the care that
implies.

## What this agent is

A crypto trading *decision support and execution* tool, operated through
Claude Code:

- Python (`robinhood_crypto_agent`) computes market analysis, a regime-aware
  multi-signal strategy, risk checks, and position sizing — producing
  `TradeProposal`s. It never talks to Robinhood directly.
- The `robinhood-trading` MCP server is the **only** trusted interface to
  account state, live quotes, and order placement/cancellation.
- You (Claude), operating in an interactive session, are the bridge between
  the two: you call the MCP tools, and you call the CLI.

## Hard rules for operating this agent

1. **Never call the MCP order-submission tool without an explicit, unambiguous
   human instruction identifying which proposal to execute by its proposal
   ID.** "That looks good" or general enthusiasm is not approval. Ask for the
   ID if it's not clear.
2. **Never execute a proposal whose `risk_check.passed` is `false`** unless the
   human uses an explicit override phrase (`OVERRIDE RISK CHECK`) in the same
   message as the approval. If overridden, log it as an override
   (`log-execution --override`), not a normal execution.
3. **Always show the risk engine's result** (pass/fail and why) before
   offering to execute anything — never execute silently.
4. **A positive backtest is not authorization to skip or loosen a risk limit
   or the propose-only approval gate.** Backtests validate a signal against
   history; they say nothing about a specific live trade.
5. **Log everything.** Every proposal (including risk-rejected ones), every
   execution attempt (success or failure) — before and after the MCP call.
   Nothing trades outside `data/audit_log.jsonl`. Use the CLI
   (`log-execution`) to write execution records; never hand-edit the log.
6. **Never change `execution_mode` from `PROPOSE_ONLY` to `AUTO`** on your own
   initiative. That is a deliberate, separately-confirmed human decision to
   enable unattended live trading, made by editing `config.py`'s config
   source directly — not something inferred from a trade-approval
   conversation.
7. **Never edit `config/risk_limits.yaml`, `config/watchlist.yaml`, or
   `config/strategy_weights.yaml`** as a side effect of executing a trade or
   running a backtest. Config changes are their own deliberate act, initiated
   explicitly by the human.
8. **Respect the kill switch.** If `robinhood-crypto-agent status` reports it
   engaged (manually, or auto-engaged by a daily-loss-cap breach), refuse all
   executions and say why. Do not try to work around it.
9. **Re-confirm the live price via MCP immediately before submitting an
   order**, and compare it to the proposal's reference price. If it has moved
   beyond `price_drift_tolerance_pct`, refuse and ask for a fresh proposal
   rather than submitting against a stale quote.
10. **Never fabricate MCP responses, balances, quotes, order confirmations, or
    backtest results.** Only real tool output goes into the audit log or a
    report.
11. **Spot crypto only.** Do not use margin, leverage, or derivative products
    even if the MCP server exposes them, unless the human explicitly asks for
    that scope to change.

## Typical workflow

```
robinhood-crypto-agent status                      # confirm mode, kill-switch, today's exposure/loss
# (fetch watchlist candles via MCP, save as JSON)
robinhood-crypto-agent analyze --data-source mcp-json candles.json
# review the proposal report + audit log
# human approves a specific proposal ID
# (re-confirm live quote via MCP, then call the MCP order tool with exactly
#  that proposal's symbol/side/quantity/order type)
robinhood-crypto-agent log-execution --proposal-id <id> --mcp-response response.json
```

Before trusting a strategy change with live proposals, validate it first:

```
robinhood-crypto-agent backtest --symbols BTC,ETH,SOL --start 2023-01-01 --end 2024-01-01
```
