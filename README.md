# robinhood-crypto-agent

A crypto trading agent for Robinhood, operated through
[Claude Code](https://claude.com/claude-code) and the **RobinHood MCP server**.

> ⚠️ **This trades real money.** `place_crypto_order` places a real order
> against a real account. There is no paper-trading endpoint to point at.
> Read [`CLAUDE.md`](./CLAUDE.md) and [`docs/risk-controls.md`](./docs/risk-controls.md)
> before running this against a funded account.

## The shape of the thing

Claude holds the MCP connection. This Python package holds the decision logic,
the risk limits, and the audit trail — and **cannot reach the network at all**.

```
     ┌──────────────────────── Claude Code ────────────────────────┐
     │  calls RobinHood MCP tools       runs the rhca CLI          │
     └───────┬──────────────────────────────────┬─────────────────-┘
             │ JSON responses                   │ proposals, payloads
             ▼                                  ▼
   get_crypto_quotes ──► rhca ingest ──► price history ──► strategy
   get_currency_pairs                                          │
   get_crypto_positions                                        ▼
   get_portfolio                                    sizing ──► risk engine
                                                                │
   place_crypto_order ◄── human approves by id ◄── rhca approve ┘
             │
             └──► rhca record-execution ──► append-only audit log
```

The split is the safety property: no code path in this repository can place an
order. Submitting one requires Claude to call an MCP tool, and the CLI only
hands it a payload after a human has approved a **specific proposal by id**.

## What it does

- **Builds its own price history.** The MCP server has no crypto historicals
  tool — only live quotes. So the agent records every quote it is given and
  aggregates bars from them. This is the central design constraint; see
  [`docs/data-constraints.md`](./docs/data-constraints.md).
- **Reads the market by regime.** ADX separates trending from ranging, and the
  signal weights change accordingly — trend following and mean reversion are
  near-opposites, so blending them at fixed weights averages out to noise.
- **Sizes by conviction and volatility.** Three multiplicative factors, each
  bounded at 1.0, so the result can never exceed the per-trade cap.
- **Refuses, loudly and specifically.** Sixteen risk rules run on every
  proposal — all of them, so the report names every blocker rather than the
  first. See [`docs/risk-controls.md`](./docs/risk-controls.md).
- **Validates order payloads offline.** The RobinHood order contract is
  encoded as checkable rules, so a malformed order fails here instead of at
  Robinhood. See [`docs/architecture.md`](./docs/architecture.md#the-contract-layer).
- **Logs everything, append-only.** Including proposals the risk engine
  blocked — that record is the evidence the controls do anything.
- **Scores itself.** Every proposal is measured against what the price actually
  did over a fixed horizon, past a hurdle set above the round-trip spread. A
  gain smaller than the spread is not a win. With nothing resolved the hit rate
  reads *unknown*, never 0%.
- **Has a web dashboard** ([`dashboard/`](./dashboard)) for reviewing proposals,
  seeing the measured hit rate, and accepting or declining — which records a
  decision the agent replays through the same approval gate, never an order.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 245 tests, no network, no account needed
ruff check src tests

export RHCA_RHS_ACCOUNT_NUMBER=...   # the NUMERIC rhs_account_number
rhca status
```

CI runs the suite on Python 3.10, 3.11, 3.12 and 3.13, plus `ruff` and a smoke
test of the installed `rhca` console script — the suite imports the package
directly, so the console-script step is the only check that the packaging
metadata and entry point actually work.

`rhca status` works on a fresh checkout and will tell you, correctly, that it
has no price history and cannot evaluate anything yet.

## Usage

Every command that consumes MCP output takes JSON on a path or on stdin, so a
tool response is never retyped or paraphrased.

```bash
# 1. Ingest what Claude fetched
rhca ingest accounts  -f accounts.json     # resolves rhs_account_number
rhca ingest pairs     -f pairs.json        # increments, halts, market-only flags
rhca ingest portfolio -f portfolio.json    # enables the concentration limit
rhca ingest quotes    -f quotes.json       # also appends to the price history

# 2. Build history: either poll quotes over time, or bootstrap from OHLC bars
rhca import-history BTC-USD -f bars.json

# 3. Analyze
rhca analyze -v

# 4. Preview, approve, place, record
rhca plan-order <proposal-id>
rhca approve <proposal-id> --approval "execute <proposal-id>" --quote fresh.json
rhca record-execution <proposal-id> --tranche 0 -f response.json

# How good have the suggestions been?
rhca accuracy

# Push proposals + outcomes to the dashboard, pull back your accept/decline
rhca dashboard-sync --url https://your-project.vercel.app --token "$TOKEN"

# Housekeeping
rhca status
rhca audit --proposal-id <proposal-id>
rhca record-pnl -f realized_pnl.json
rhca kill-switch on --reason "stepping away"
rhca describe-tools
rhca validate-order -f payload.json
```

See [`docs/runbook.md`](./docs/runbook.md) for the full operating loop, and
[`docs/autonomy.md`](./docs/autonomy.md) for the route from supervised
sign-off to unattended execution.

## Repository layout

```
src/robinhood_crypto_agent/
├── cli.py              # the command surface Claude drives
├── agent.py            # the analysis pipeline
├── config.py           # config, clamped by hard code ceilings
├── models.py           # domain types
├── numeric.py          # Decimal helpers; no price ever becomes a float
├── symbols.py          # BTCUSD vs BTC-USD reconciliation
├── indicators.py       # SMA/EMA/RSI/MACD/ATR/ADX/Bollinger/Donchian
├── risk.py             # the 16 risk rules
├── sizing.py           # conviction x volatility x caps
├── audit.py            # append-only log; the daily caps read from it
├── serde.py            # proposal round-trip through the log
├── reports.py          # human-readable output
├── outcomes.py         # scoring proposals against what the price did next
├── decisions.py        # accept/decline records from the dashboard
├── dashboard.py        # the payload the dashboard renders
├── mcp/                # the RobinHood tool contract and response parsers
├── store/              # price history and cached account state
├── strategy/           # signals, regime detection, composite blending
└── execution/          # plans, order payloads, approval gate, kill switch

dashboard/              # Next.js app, deployable to Vercel
```

## Status

**Phase 1 (today): analyze and propose.** The agent prepares a trade, you sign
it off by naming its proposal id, and it submits. Every control in
[`docs/risk-controls.md`](./docs/risk-controls.md) is live.

**Phase 2 (intended): autonomous execution.** `execution_mode: auto` is the
destination, not a dead end — but it is refused until the promotion criteria in
[`docs/autonomy.md`](./docs/autonomy.md) are met, and setting it today only
makes every proposal fail the `execution_mode` risk check.

The good news for Phase 2 is that only *one* step is human-shaped. Sizing, the
16 risk rules, the kill switch, the price-drift re-check, the
remaining-quantity accounting and the audit-log daily caps all already run
without a human. Phase 2 swaps the authorization source; it does not rework the
pipeline.

**Next up.** Running the ingest → analyze → sync loop on an hourly schedule (so
the hit rate is measured against real trading rather than a backfill), and a
round of dashboard UX work. Both are written up with their gotchas in
[`docs/roadmap.md`](./docs/roadmap.md).
