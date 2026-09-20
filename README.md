# robinhood-crypto-agent

A Robinhood crypto trading agent operated through [Claude Code](https://claude.com/claude-code).

> ⚠️ **This trades real money.** There is no paper-trading mode. Read
> [`CLAUDE.md`](./CLAUDE.md) and [`docs/risk-controls.md`](./docs/risk-controls.md)
> before running anything against a live account.

## What it is

- **Execution**: all account access, quotes, and order placement go through
  the `robinhood-trading` MCP server (see [`.mcp.json`](./.mcp.json)) — never
  a direct API integration. Order submission only ever happens as an
  explicit MCP tool call made by Claude inside an interactive session,
  triggered by a human naming a specific approved proposal.
- **Decision-making**: a regime-aware, multi-signal strategy (trend,
  mean-reversion, volume, cross-asset relative strength, and a sentiment
  signal from the free Fear & Greed Index) blended by detected market regime
  (trending vs. ranging), with volatility-scaled position sizing. See
  [`docs/strategy.md`](./docs/strategy.md).
- **Safety**: starts in **analyze-and-propose-only** mode — the agent never
  executes a trade on its own. Every proposal passes through hard-coded risk
  limits (position size, daily loss/exposure caps, watchlist allowlist, a
  kill switch) before a human can even consider approving it. See
  [`docs/risk-controls.md`](./docs/risk-controls.md).
- **Backtesting**: strategy changes are validated via walk-forward
  backtesting with a fee/slippage/spread cost model against historical data
  before they're trusted to generate live proposals. A backtest is a
  precondition for trusting a signal, not a license to skip any risk limit.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

claude mcp list   # confirm `robinhood-trading` is registered (see .mcp.json)
```

## Usage

```bash
# Check current mode, kill-switch state, and today's risk usage
robinhood-crypto-agent status

# Validate the strategy against history before trusting it
robinhood-crypto-agent backtest --symbols BTC,ETH,SOL --start 2023-01-01 --end 2024-01-01

# Generate proposals (run inside a Claude Code session so watchlist candles
# can be pulled live via MCP; --data-source public uses free historical data
# for offline iteration instead)
robinhood-crypto-agent analyze --data-source mcp-json candles.json

# Inspect the audit trail
robinhood-crypto-agent show-audit --date 2024-01-01

# After a human-approved execution, record the real MCP response
robinhood-crypto-agent log-execution --proposal-id <id> --mcp-response response.json

# Manual kill switch
robinhood-crypto-agent kill-switch on
robinhood-crypto-agent kill-switch off
```

See [`docs/runbook.md`](./docs/runbook.md) for the full manual end-to-end
verification checklist.

## Repository layout

```
src/robinhood_crypto_agent/
├── cli.py               # analyze | backtest | log-execution | show-audit | status | kill-switch
├── config.py             # risk limits, watchlist, execution mode
├── models.py              # Candle, Signal, TradeProposal, RiskCheckResult, ExecutionRecord
├── strategy/               # regime detection, indicators, signal sources, composite blend
├── sizing/                  # volatility-scaled position sizing
├── risk/                     # hard risk limits + evaluation engine
├── execution/                  # the human-approval execution gate + kill switch
├── audit/                       # append-only JSONL audit log
├── backtest/                     # walk-forward simulator, cost model, metrics
├── data/                          # public OHLCV + Fear & Greed Index clients
└── reports/                        # human-readable proposal reports
```

## Status

Phase 1: analyze-and-propose-only. Automated execution is an explicit future
phase, not implemented — see `execution/adapter.py` for the extension point.
