# Risk controls

This agent trades real money with no paper-trading layer. The controls below
are what stand between a strategy signal and an actual order.

## Layered defenses

1. **Analyze-and-propose-only mode.** No order is ever submitted without an
   explicit human instruction naming a specific proposal ID. This is the
   primary safety gate in Phase 1 (see `execution/adapter.py`).
2. **Hard risk limits** (`config/risk_limits.yaml`, enforced by
   `risk/engine.py`):
   - `max_position_usd` — per-trade notional cap.
   - `max_daily_loss_usd` — realized loss cap, computed from today's
     `ExecutionRecord`s in the audit log; breaching it auto-engages the kill
     switch for the rest of the day.
   - `max_daily_exposure_usd` — cumulative notional of today's
     proposed/executed trades.
   - `max_open_positions`.
   - `allowed_symbols` (derived from `config/watchlist.yaml`) — any symbol
     not listed is hard-rejected regardless of what the strategy or a human
     asks for.
3. **Absolute code-level ceilings** (`risk/limits.py`) — `RiskLimits`
   validation rejects a YAML value that exceeds a hard-coded ceiling, so a
   careless config edit can't silently blow past what the code considers
   sane, even before a human reviews it.
4. **Kill switch** (`execution/kill_switch.py`) — a file-based flag,
   settable manually (`kill-switch on|off`) or auto-engaged by a daily-loss
   breach. While engaged, all executions are refused.
5. **Price-drift check** — immediately before submitting an approved order,
   Claude re-confirms the live quote via MCP and refuses if it has moved
   beyond `price_drift_tolerance_pct` from the proposal's reference price.
6. **Append-only audit log** (`audit/log.py`) — every proposal (including
   rejected ones), every execution attempt, is recorded. The daily
   loss/exposure caps are computed *from* this log, so it's both the record
   and the enforcement data source.

## Current starting values (Conservative preset)

| Limit | Value |
|---|---|
| Max position per trade | $50 |
| Max daily realized loss | $100 |
| Max daily exposure | $250 |
| Max open positions | 3 |
| Price drift tolerance | 0.5% |

These are intentionally small for a new, unproven agent. Loosen them
deliberately and gradually — never as a side effect of wanting one specific
trade to clear.

## Known gap: realized-loss tracking depends on MCP data

`max_daily_loss_usd` is computed from `realized_pnl_usd` on logged
`ExecutionRecord`s. That field is only populated when `log-execution` is
given a real realized-P&L figure from the MCP order response (typical for a
closing/reducing trade). If the MCP server's response doesn't surface
realized P&L for a given order, that execution contributes $0 to the daily
loss figure — the cap can under-count losses in that case. Treat the printed
daily-loss figure from `status` as a floor, not a guarantee, until this is
confirmed against what the MCP server actually returns, and keep manually
reviewing account performance in the interim.

## What a backtest does and doesn't prove

A walk-forward backtest with realistic costs (`backtest/`) validates that a
strategy had positive expectancy on historical data under a cost model. It
does **not** guarantee future performance, and it is never grounds to skip a
risk limit or the propose-only approval gate for a live trade. Recommended
practice: shadow-run `analyze` and compare proposals to actual subsequent
price action for a period before increasing size or considering the
automated-execution phase.
