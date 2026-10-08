# robinhood-crypto-agent

A crypto trading agent for Robinhood that trades one rule on its watchlist
(BTC, ETH, SOL and XRP): **the split**. Half the account is bought and held
long-term -- a tranche at a time, only on a close under the 200-day average --
and half trades the **breakout**: it buys a daily close above the prior
20-day high and the 100-day average, sized by risk, and sells on a trailing
stop. Fourteen risk rules check every proposal. Orders go only through
[Claude Code](https://claude.com/claude-code) and the **RobinHood MCP
server**, after a human approves each one by id.

> ⚠️ **This trades real money.** `place_crypto_order` places a real order
> against a real account. There is no paper-trading endpoint to point at.
> Read [`CLAUDE.md`](./CLAUDE.md) and [`docs/risk-controls.md`](./docs/risk-controls.md)
> before running this against a funded account.

## The shape of the thing

Claude holds the MCP connection. This Python package holds the decision logic,
the risk limits, and the audit trail. It can *read* from Robinhood (see
`rhca run` below), but **it has no code path that places an order**.

```
     ┌──────────────────────── Claude Code ────────────────────────┐
     │  calls RobinHood MCP tools       runs the rhca CLI          │
     └───────┬──────────────────────────────────┬─────────────────-┘
             │ JSON responses                   │ proposals, payloads
             ▼                                  ▼
   get_crypto_quotes ──► rhca ingest ──► daily closes  ──► the split   
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

### The real-time loop: `rhca run` (shadow mode)

```
Coinbase daily closes ─► the split ─► 14 risk rules ─► proposal (priced off
                            ▲                               │   Robinhood's quote)
     recorded fills ────────┘                               ▼
     (each sleeve's state)                     audit log ─► dashboard
```

`rhca run` polls Robinhood's Crypto Trading API with a **read-only** client (no
order method exists), for quotes and trading pairs only. The balance and
holdings it checks against are the Agentic account's, which Claude Code feeds
in with `rhca ingest`. After each UTC daily close it runs the split, and logs
what it wants as a proposal for a human to approve. Setup and keys:
[`docs/runbook.md`](./docs/runbook.md#shadow-run-rhca-run).

It also keeps the **forward test**: the same split on paper from 2026-09-28,
recorded after each UTC daily close and reported by `rhca shadow`. It never
proposes ([`docs/strategy.md`](./docs/strategy.md#the-forward-test)).

## What it does

- **Builds its own price history.** The MCP server has no crypto historicals
  tool — only live quotes. So the agent records every quote it is given and
  aggregates bars from them. This is the central design constraint; see
  [`docs/data-constraints.md`](./docs/data-constraints.md).
- **Trades one backtested rule.** The split
  ([`docs/strategy.md`](./docs/strategy.md#the-split-long-term--short-term)):
  $250 long-term in ten tranches a coin, $250 in the breakout, no coin over
  20% of the account. `rhca backtest --strategies split` replays the same
  rules, and a test holds the live path to the backtest's trades.
- **Rebuilds its state from what filled.** Each sleeve's cash, holdings and
  entry days come from the fills recorded in the audit log, so a restart
  changes nothing and the split never sells a coin it did not buy.
- **Refuses, loudly and specifically.** Fourteen risk rules run on every
  proposal — all of them, so the report names every blocker rather than the
  first. See [`docs/risk-controls.md`](./docs/risk-controls.md).
- **Validates order payloads offline.** The RobinHood order contract is
  encoded as checkable rules, so a malformed order fails here instead of at
  Robinhood. See [`docs/architecture.md`](./docs/architecture.md#the-contract-layer).
- **Logs everything, append-only.** Including proposals the risk engine
  blocked — that record is the evidence the controls do anything.
- **Reports its P&L.** `rhca status` shows each sleeve's cash, holdings, cost
  and realized P&L from the recorded fills. (The six-hour hit rate in
  `rhca accuracy` scored the retired System 1's predictions; the split holds
  for days or for good, so it is measured on P&L instead.)
- **Has a web dashboard** ([`dashboard/`](./dashboard)) for reviewing proposals
  and accepting or declining — which records a decision the agent replays
  through the same approval gate, never an order.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # all offline, no account needed
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
# The real-time loop (keys in .env -- see .env.example)
rhca bootstrap-history          # 52 days of hourly Coinbase bars, for the charts
rhca run --once                 # one pass of every task: the smoke test
rhca run --keep-awake           # shadow mode until Ctrl+C

# 1. Ingest what Claude fetched
rhca ingest accounts  -f accounts.json     # resolves rhs_account_number
rhca ingest pairs     -f pairs.json        # increments, halts, market-only flags
rhca ingest portfolio -f portfolio.json    # Agentic value + crypto buying power
rhca ingest quotes    -f quotes.json       # also appends to the price history

# 2. Build history: either poll quotes over time, or bootstrap from OHLC bars
rhca import-history BTC-USD -f bars.json

# 3. Analyze
rhca analyze -v

# 4. Preview, approve, place, record
rhca plan-order <proposal-id>
rhca approve <proposal-id> --approval "execute <proposal-id>" --quote fresh.json
rhca record-execution <proposal-id> --tranche 0 -f response.json

# Replay the retired ladder, the breakout and their baselines on Coinbase
# bars, over the whole window and over rolling 90-day windows
rhca backtest --days 730 --roll-window 90
rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 730 --strategies breakout
# ... and the split the agent trades: half held long-term, half in the breakout
rhca backtest --days 1460 --roll-window 90 --strategies split

# The forward test: the split on paper since 2026-09-28, against its bar
rhca shadow

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
├── cli.py              # the command surface
├── runner.py           # rhca run: the real-time loop, shadow mode
├── robinhood.py        # read-only Crypto Trading API client (no order methods)
├── bootstrap.py        # Coinbase candles for a fresh checkout
├── backtest.py         # rhca backtest: the retired ladder vs its baselines
├── portfolio_backtest.py # the breakout and the split, as one account across coins
├── shadow.py           # the forward test: the split on paper, one daily close at a time
├── net.py              # the one HTTP helper
├── agent.py            # the analysis pipeline: daily closes + ledger -> split -> proposals
├── ledger.py           # each sleeve's cash and holdings, from recorded fills
├── daily.py            # Coinbase daily closes, cached: what the split decides on
├── config.py           # config, clamped by hard code ceilings
├── models.py           # domain types
├── numeric.py          # Decimal helpers; no price ever becomes a float
├── symbols.py          # BTCUSD vs BTC-USD reconciliation
├── risk.py             # the 14 risk rules
├── sizing.py           # a step's dollars -> a quantity the pair accepts
├── audit.py            # append-only log; the daily caps read from it
├── serde.py            # proposal round-trip through the log
├── reports.py          # human-readable output
├── outcomes.py         # scoring System 1's old proposals, for the record
├── decisions.py        # accept/decline records from the dashboard
├── dashboard.py        # the payload the dashboard renders
├── mcp/                # the RobinHood tool contract and response parsers
├── store/              # price history and cached account state
├── strategy/           # the split (live): breakout + hodl; the retired ladder
└── execution/          # order payloads, approval gate, kill switch

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
14 risk rules, the kill switch, the price-drift re-check, the
remaining-quantity accounting and the audit-log daily caps all already run
without a human. Phase 2 swaps the authorization source; it does not rework the
pipeline.

**Next up.** The split is live, by the owner's decision on 2026-09-28: every
proposal still waits for approval by id. The forward test's bar is read on
2026-12-27. See [`docs/roadmap.md`](./docs/roadmap.md).
