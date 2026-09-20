# Session Handoff

Last updated: 2026-09-20, by Claude Code session `session_01MKanTJyk7b8dauBqLoVH2v`.

## Current state

- **Branch**: `claude/robinhood-crypto-agent-h28gd4`
- **PR**: [#1 — Add initial Robinhood crypto trading agent](https://github.com/joman124/robinhood-crypto-agent/pull/1) — **open, unmerged**, 2 commits, mergeable/clean, no CI configured on the repo, no reviews yet.
- **Tests**: 97 passing (`pytest`, no network required). `ruff check` clean aside from a few pre-existing stylistic `dict()`-literal nits in test helper functions (not fixed, low priority).
- Not currently watching the PR — a prior session subscribed to GitHub activity and a self-scheduled check-in; both were explicitly cancelled on request ("stop running"). Re-subscribe if you want CI/review events pushed to a session again.

## What this repo is

A Robinhood **spot crypto** trading agent operated through Claude Code. Full design rationale lives in `docs/architecture.md`, `docs/strategy.md`, `docs/risk-controls.md`, and operating rules for Claude live in `CLAUDE.md` — **read `CLAUDE.md` first in any new session**, it's the load-bearing document.

One-paragraph summary: Python (`src/robinhood_crypto_agent/`) computes a regime-aware, multi-signal strategy and risk-checks/sizes a `TradeProposal`, but never talks to Robinhood directly. The `robinhood-trading` MCP server (`.mcp.json`) is the only trusted path to account data/quotes/orders, and it's called only by Claude, only after a human approves a specific proposal by ID. Nothing in the codebase can submit an order on its own (`execution/adapter.py`).

## Key decisions already made (don't re-litigate without reason)

- **Real money, no paper-trading mode** — explicit user choice early on. Propose-only execution is the actual safety gate.
- **Spot only** — no margin/leverage/derivatives, even if the MCP server exposes them.
- **Conservative risk preset**: $50/trade, $100/day realized loss, $250/day exposure, max 3 open positions (`config/risk_limits.yaml`), bounded by absolute code-level ceilings in `risk/limits.py`.
- **Watchlist**: BTC, ETH, SOL, DOGE, LTC, BCH, AVAX, LINK, ADA, SHIB (`config/watchlist.yaml`).
- **Strategy**: regime detection (ADX-based TRENDING/RANGING) gates a weighted blend of 5 independent signals (trend, mean-reversion, volume, cross-asset relative strength, Fear & Greed sentiment) — see `docs/strategy.md`.
- **Execution planning** (latest addition): every proposal also gets an `ExecutionPlan` (`execution/plan.py`) derived from its regime — TRENDING → submit promptly as one order; RANGING → stage 3 limit-order tranches at increasingly favorable prices. This came from comparing four Polymarket-bot strategies (mo-money, almach, 0xb55, Bonereaper) for transferable ideas; only mo-money's timeframe-conditional behavior and almach's passive-accumulation technique survived the trip to spot crypto (0xb55's and Bonereaper's edges depend on conditional-token/HFT mechanics that don't exist here). It's informational only — never touches the approval gate or risk evaluation.
- **Backtesting is part of the base build, not deferred** — walk-forward-style simulator with a fee/slippage/spread cost model (`backtest/`), validated against free CoinGecko + Alternative.me Fear & Greed data.

## Known gaps / things to double check next session

1. **MCP endpoint never independently verified.** `.mcp.json` points at `https://agent.robinhood.com/mcp/trading`. Every sandbox session so far has had egress to that domain blocked, so it's only ever been verified by the repo owner's own say-so. Worth a final confirmation before any live trading.
2. **A *different*, already-authenticated MCP server named `RobinHood`** (not `robinhood-trading`) appeared in this session's tool list with a large set of live trading tools (`place_crypto_order`, `get_crypto_positions`, `place_equity_order`, options trading, etc.). This was noticed but **not investigated or used** — it's unclear whether it's the same account/integration as `.mcp.json`'s `robinhood-trading`, a different official Robinhood MCP offering, or something else entirely. **Do not assume it's equivalent or safe to use in place of `robinhood-trading` without the user explicitly confirming what it is** — `CLAUDE.md` currently names `robinhood-trading` as the *only* trusted interface, and that server also exposes equities/options, which are out of this project's stated spot-crypto-only scope.
3. **`robinhood-trading` itself requires OAuth authorization** that hasn't happened in any sandbox session (non-interactive sessions can't run the flow) — confirm it's been authorized via `claude mcp` / `/mcp` in an interactive session before attempting the live runbook.
4. **Realized-loss tracking has a documented gap**: `max_daily_loss_usd` enforcement depends on the MCP order response including `realized_pnl_usd` for closing trades. If the real MCP server doesn't surface that field, the daily-loss cap under-counts. See `docs/risk-controls.md` → "Known gap: realized-loss tracking depends on MCP data".
5. **No live end-to-end run has happened yet.** Everything is validated against fixtures/free public data only. The full manual runbook is in `docs/runbook.md` — nobody has executed it against the real MCP server.
6. **`execution_mode` has never been flipped to `AUTO`** — there is no automated-execution adapter implementation yet, only the extension point (`execution/adapter.py`). Building one is explicitly a deliberate future phase, not something to infer from context.

## How to pick this back up

```bash
git clone <repo> && cd robinhood-crypto-agent
git checkout claude/robinhood-crypto-agent-h28gd4   # or main, if PR #1 has since merged
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                              # should show 97+ passing, no network needed
claude mcp list                                     # confirm robinhood-trading is registered/authorized
```

Then read `CLAUDE.md`, `docs/architecture.md`, and `docs/strategy.md` in that order before doing anything with live money.

## Useful links

- PR: https://github.com/joman124/robinhood-crypto-agent/pull/1
- This session's transcript: https://claude.ai/code/session_01MKanTJyk7b8dauBqLoVH2v
