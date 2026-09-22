# Runbook

## Shadow run (`rhca run`)

The real-time loop: Robinhood quotes and RSS news in, Jev labels the news,
System 1 scores every symbol, strong candidates go to Claude Sonnet 5, and
everything is logged and scored. **It cannot place an order** — the Robinhood
client has no order method. It runs on this PC, in a terminal you leave open.

### 1. Keys (you create these; nothing else can)

| Variable | Where | Needed for |
|---|---|---|
| `ROBINHOOD_API_KEY`, `ROBINHOOD_PRIVATE_KEY` | Robinhood's crypto API settings; the private key is the base64 32-byte Ed25519 seed | **Required** — quotes, pairs, holdings |
| `TYPESAFE_API_KEY` | console.typesafe.ai (Jev is early access) | News labels |
| `ANTHROPIC_API_KEY` | console.anthropic.com | System 2 |

Copy `.env.example` to `.env` in the repo root and fill it in. `.env` is
gitignored, and variables already set in your environment win over it. If the
Robinhood key setup offers read-only permissions, choose them: the shadow run
never needs more.

### 2. Start

From the repo root. Calling the venv's `rhca` directly works the same in
PowerShell and cmd, and needs no activation script:

```powershell
.venv\Scripts\rhca bootstrap-history   # ~12 days of Coinbase bars, so indicators work at once
.venv\Scripts\rhca run --once          # one pass of every task, then exit: the smoke test
.venv\Scripts\rhca run --keep-awake    # the real run; Ctrl+C stops it
```

Run `bootstrap-history` right before starting, every time: it only fills bars
that are missing, and a gap between the last imported bar and the first polled
one would read to the indicators as a single long bar.

`--keep-awake` asks Windows not to sleep while `rhca run` is open, and the
request ends with the process. Without it, sleep pauses the loop, and
`rhca status` shows the heartbeat going stale.

### 3. Check the first `--once` output

The Robinhood REST response shapes were written from Robinhood's docs and have
not yet been confirmed against a live account. The first run is that check:

- [ ] No `quotes:` or `account:` warnings. `data/market_state.json` shows
      sensible bids and asks, with the ask above the bid.
- [ ] No `... is reported untradable or halted` warning. If every pair gets
      one, the pair's `status` field came back spelled differently from the
      docs. Every proposal would then be blocked by `pair_tradable`, so fix the
      parser before running on.
- [ ] `data/news.jsonl` has headlines with `labels`, if Jev is on.
- [ ] The log lists candidates as either `held back: ...` or
      `escalated -> System 2 ...`.

Once it has run, replace the invented REST payloads in
`tests/unit/test_robinhood_client.py` with trimmed live captures.

### 4. Watch it

```powershell
.venv\Scripts\rhca status                 # pipeline RUNNING/NOT RUNNING, counts, last error, keys
.venv\Scripts\rhca audit --kind proposal  # every candidate, with trigger_reason and system2_decision
.venv\Scripts\rhca accuracy               # hit rate, now also by status (see below)
```

Every System 1 candidate is logged once per idea: once per side and closed bar,
plus once more whenever a new headline changes the view. Each is logged with a
status:

| Status | Meaning |
|---|---|
| `proposed` | Passed risk and the trigger, and System 2 said propose. It gets an Accept button on the dashboard. |
| `declined_by_system2` | System 2 said pass, or could not answer. |
| `not_escalated` | Passed risk but not the trigger (threshold, cooldown or daily cap). |
| `rejected_by_risk` | Blocked by one of the 16 rules. |

All four are scored against what the price did next. `rhca accuracy` groups
them by status, so the run answers two questions. Does System 2 add anything
(`proposed` vs `declined_by_system2`)? Is the trigger in the right place
(escalated vs `not_escalated`)?

A `proposed` row is still only a proposal. Acting on it is the normal flow
below: a human names the id, `rhca approve` re-checks everything, and Claude
Code places the order through MCP.

### Spend

- **Sonnet 5:** at most `max_escalations_per_day` (24) conversations a day. My
  estimate is $0.05–0.15 each, so about $3.60/day at worst.
- **Jev:** fractions of a cent per day.
- **Crypto.com market data:** free, and needs no key. Sonnet reads it through
  the Anthropic API's MCP connector, and each lookup's result is billed as
  Sonnet input tokens.

Tune all of these in `config/pipeline.yaml`.

## First run on a funded account

Before anything touches real money:

```bash
pip install -e ".[dev]" && pytest        # all offline
rhca describe-tools                      # confirm which tools mutate
rhca status                              # should say: no history, switch released
```

Then, in Claude Code with the `RobinHood` MCP server connected:

1. **Resolve the account.** Call `get_accounts`, pipe into
   `rhca ingest accounts`. Confirm the printed `rhs_account_number` is the
   **numeric** one, and export it:
   `export RHCA_RHS_ACCOUNT_NUMBER=<that value>`.
2. **Confirm the switch works.** `rhca kill-switch on`, then try
   `rhca approve` on anything — it must refuse. `rhca kill-switch off`.
3. **Check the contract guard.** Feed `rhca validate-order` a payload with the
   *alphanumeric* `account_number` and confirm it is rejected.

## Daily loop

### Ingest
```bash
# get_currency_pairs  -> increments, halts, market-only flags
rhca ingest pairs     -f pairs.json
# get_portfolio       -> enables the concentration limit
rhca ingest portfolio -f portfolio.json
# get_crypto_positions-> enables sell coverage and the open-position cap
rhca ingest positions -f positions.json
# get_crypto_quotes   -> also appends to the price history
rhca ingest quotes    -f quotes.json
```

Ingest quotes on a schedule. Roughly four times per bar interval is where
sampling stops limiting signal confidence — see
[`data-constraints.md`](./data-constraints.md).

### Analyze
```bash
rhca analyze -v
```

Read the risk verdict **before** the trade details. Exit code 2 means nothing
is executable — usually correct, not a problem to work around.

### Execute one proposal
```bash
rhca plan-order <id>                     # preview payloads
# call preview_crypto_order; show the user the estimated cost and fees
# re-fetch get_crypto_quotes -> fresh.json
rhca approve <id> --approval "<the user's exact words>" --quote fresh.json
# call place_crypto_order with the printed payload, verbatim
rhca record-execution <id> --tranche 0 -f response.json
```

For a `STAGED` plan, repeat `approve` / `place` / `record-execution` per
tranche, incrementing `--tranche`. An unfilled tranche is expected — do not
chase it.

### Close the day
```bash
# call get_realized_pnl
rhca record-pnl -f realized_pnl.json
rhca status
```

## Reconciling against Robinhood

The audit log records what the agent was told. To check it against what
Robinhood actually holds, call `get_crypto_orders` and `get_crypto_positions`
and compare by hand against:

```bash
rhca audit --proposal-id <id>     # per proposal: fills, states, totals
rhca audit --date YYYY-MM-DD      # the day's events
```

If they disagree, the audit log is wrong — Robinhood is authoritative. Record
the missing execution rather than editing the log; a hand-edited log raises an
error on the next read.

## Incidents

**A fill was never recorded.** The daily budget is now overstated. Fetch the
order via `get_crypto_orders --order-id`, and `record-execution` it against the
proposal id. If the proposal id is genuinely unknown, engage the kill switch
and reconcile before trading again.

**A tranche filled twice.** `rhca approve` refuses a tranche exceeding the
remaining quantity, so this means an order was placed without going through the
gate. Engage the kill switch, reconcile positions, and re-read `CLAUDE.md`
rule 2.

**Prices look wrong.** Check `rhca status` for the observation age. A quote
older than `max_quote_age_seconds` blocks proposals by design.

**The switch auto-engaged.** The daily loss cap was reached. It stays engaged
until released by hand — that pause is the control working. Do not release it
to "make back" the loss.

## Pre-flight checklist for a strategy change

- [ ] `pytest` passes
- [ ] `rhca analyze --no-record` on imported history shows the expected
      behaviour change
- [ ] The risk verdicts in the report still name every rule
- [ ] No risk limit was edited as part of the change
- [ ] `config/*.yaml` still loads (`rhca status` exits 0)
