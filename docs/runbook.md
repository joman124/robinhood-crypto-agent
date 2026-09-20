# Runbook

## First run on a funded account

Before anything touches real money:

```bash
pip install -e ".[dev]" && pytest        # 245 tests, all offline
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
