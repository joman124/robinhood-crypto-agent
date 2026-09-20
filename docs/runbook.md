# Manual end-to-end verification runbook

Run this after any change that touches execution, risk limits, or the MCP
integration, before trusting the agent with a live proposal.

1. `claude mcp list` — confirm `robinhood-trading` is registered.
2. `pytest` — all unit and integration tests pass, including
   `test_execution_adapter.py` (asserts the Phase 1 adapter can never submit
   an order).
3. `robinhood-crypto-agent backtest --symbols BTC,ETH,SOL --start 2023-01-01 --end 2024-01-01`
   — confirm it runs end-to-end against real historical data, produces a
   walk-forward report with costs included, and results look sane (no
   suspiciously perfect win rate, which usually means lookahead).
4. In a live Claude Code session, have Claude pull watchlist candles via MCP
   and run `robinhood-crypto-agent analyze --data-source mcp-json candles.json`.
5. Confirm the proposal report and `data/audit_log.jsonl` show every
   candidate, including risk-rejected ones with reasons, and that the
   regime/signal breakdown behind each proposal is visible (not a black
   box).
6. `robinhood-crypto-agent status` — confirm `execution_mode == PROPOSE_ONLY`.
7. Approve one proposal by ID; confirm Claude re-fetches a live quote via MCP
   and submits only that proposal's exact fields — nothing else.
8. Confirm `log-execution` runs with the real MCP response and the audit log
   cross-references correctly.
9. Provoke a risk violation on purpose (oversized trade, off-watchlist
   symbol) — confirm refusal at both the CLI and Claude level, and that the
   refusal itself is logged.
10. Engage the kill switch (`kill-switch on`) — confirm execution is blocked
    and logged, then disengage it.
11. `git status` / `git diff` after the session — confirm no config file
    (`.mcp.json`, `CLAUDE.md`, `config/*.yaml`) was silently modified.
