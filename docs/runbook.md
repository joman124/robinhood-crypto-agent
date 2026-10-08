# Runbook

## Shadow run (`rhca run`)

The real-time loop: Robinhood quotes in, Coinbase's daily closes, and the
split ([`strategy.md`](./strategy.md#the-split-live)) run on every watchlist
coin (BTC, ETH, SOL, XRP) after each UTC daily close. What the split wants is
logged as a proposal for you to approve.
**It cannot place an order** — the Robinhood client has no order method. It
runs on this PC, in a terminal you leave open.

It reads only market data from Robinhood. The balance and holdings it sizes
and checks against are the Agentic account's, and you feed those in through
Claude Code (step 2).

### 1. Keys (you create these; nothing else can)

Open a regular PowerShell window in the repo folder, and open `.env` in Notepad.
If `.env` doesn't exist yet, `copy .env.example .env` first.

```powershell
cd C:\Projects\robinhood-crypto-agent\robinhood-crypto-agent
notepad .env
```

Put one `NAME=value` per line, with no quotes and no spaces around the `=`.
`.env` is gitignored, and a variable already set in your environment wins over
it.

**Robinhood (required): quotes and pairs**

1. `.venv\Scripts\rhca keygen` makes the key pair. It writes
   `ROBINHOOD_PRIVATE_KEY` into `.env` without showing it, and prints the
   **public** key.
2. On a computer, sign in to Robinhood's web classic site and open
   <https://robinhood.com/account/crypto>.
3. Select **Add key**, and paste the public key from step 1.
4. Under API actions, enable only the **read-only** ones: accounts, holdings,
   products, quotes, and orders if listed as read. Leave both "place crypto
   orders" options **off**. The shadow run never needs them, and a key without
   them cannot trade even if it leaks.
5. Save. Copy the API key Robinhood shows into `.env` as
   `ROBINHOOD_API_KEY=...`.

**Which account.** A Robinhood Crypto API key only ever reads your **main**
crypto account. The key page has no account picker, and Robinhood's docs
describe access to the **Agentic** account, the one orders go to, only through
its MCP server. The loop doesn't need more. It uses the key only for market
data: quotes and trading pairs, which are the same whichever account reads
them. The Agentic account's balance and holdings come through Claude Code
instead ([step 2](#2-balance-and-holdings)).

Pin your main crypto account in `.env`, with its full number or its last 4
digits:

```
RHCA_CRYPTO_ACCOUNT=1234
```

With the pin, `rhca run` refuses to start when the key reads any other
account, which catches a key from another Robinhood login. Don't pin the
Agentic account: the key can never read it, so the run would only refuse to
start. The banner and `rhca status` show the account the key reads, as
`****1234`. Replacing a key means making a new pair: `rhca keygen --force`,
then **Add key**. Delete the old key at Robinhood once it's unused.

The loop needs no other key. (System 1's Anthropic and TypeSafe keys went
with it on 2026-09-27; delete them from `.env` if they are still there.)

**Check**

`.venv\Scripts\rhca status` lists the key *names* it found under `keys set`,
never the values. Keep `.env` to yourself: nothing ever needs it pasted
anywhere.

### 2. Balance and holdings

The loop never reads a balance. Buying power, holdings and portfolio value are
the Agentic account's, as you last ingested them. They drive the
concentration limit, sell coverage and the open-position cap, and they cap
every sell at what the account actually holds. In Claude Code, with the `RobinHood` MCP server
connected:

1. `get_accounts`: the Agentic account is the one the agent can trade. Note its
   `account_number` and its numeric `rhs_account_number`.
2. `get_portfolio` with that `account_number`, into
   `rhca ingest portfolio -f portfolio.json`. It caches the account's value and
   its crypto buying power.
3. `get_crypto_positions` with that `rhs_account_number`, into
   `rhca ingest positions -f positions.json`.

Do it at the start of each session, and again after every fill or deposit.
Holdings and buying power change only then, and only quotes are checked for
age, so the snapshot stays right in between. The account's total value still
drifts with prices, which moves the concentration limit a little between
ingests.

The `rhca run` banner shows the snapshot:
`agentic account : crypto buying power $…, N position(s), balance ingested M minutes ago`.
Until the first `ingest portfolio`, it reads `balance NEVER INGESTED`.
`rhca status` lists each section's age.

**Once, after updating to this version:** `data/market_state.json` still holds
your main account's holdings and portfolio value from earlier runs. Ingest
both before trusting a proposal: `ingest positions` replaces the old holdings
wholesale, and `ingest portfolio` the old value.

### 3. Start

From the repo root. Calling the venv's `rhca` directly works the same in
PowerShell and cmd, and needs no activation script:

```powershell
.venv\Scripts\rhca run --once          # one pass of every task, then exit: the smoke test
.venv\Scripts\rhca run --keep-awake    # the real run; Ctrl+C stops it
```

`run` fetches the missing Coinbase bars itself before it starts, so there is
nothing to remember. It fetches two kinds:

- **Daily closes** are what the split decides on. They are cached per coin
  under `data/daily/`, with enough history for the 200-day average, and the
  loop refetches each new close once Coinbase publishes it. The banner's
  `daily closes` line shows the last close on hand for each coin:
  `BTC-USD through YYYY-MM-DD, ...`.
- **Hourly bars** no longer feed the strategy. They draw the dashboard's price
  charts and score proposals for `rhca accuracy`. The first start fetches 52
  days of them (sized by the retired ladder's `strategy.trend_days`), each
  restart fills the gap since the loop last ran, and the banner's `history`
  line says how many it imported.

`--no-bootstrap` skips both. The first pass still fetches the daily closes it
needs. `rhca bootstrap-history` runs the hourly import on its own if you want
it without starting the loop.

A symbol Coinbase cannot serve is reported and skipped rather than stopping the
loop — `run` starts even when Coinbase is unreachable, just with whatever
history is already on disk.

`--keep-awake` asks Windows not to sleep while `rhca run` is open, and the
request ends with the process. Without it, sleep pauses the loop, and
`rhca status` shows the heartbeat going stale.

### 4. Check the first `--once` output

The Robinhood REST response shapes were written from Robinhood's docs and have
not yet been confirmed against a live account. The first run is that check:

- [ ] No `quotes:` or `pairs:` warnings. `data/market_state.json` shows
      sensible bids and asks, with the ask above the bid.
- [ ] No `... is reported untradable or halted` warning. If every pair gets
      one, the pair's `status` field came back spelled differently from the
      docs. Every proposal would then be blocked by `pair_tradable`, so fix the
      parser before running on.
- [ ] The banner's `daily closes` line lists `BTC-USD`, `ETH-USD`, `SOL-USD`
      and `XRP-USD`, each through the latest closed UTC day, and does not end
      in `, N failed`. Each failure prints its reason on a `!` line below it.
- [ ] `rhca analyze --no-record` gives each coin either a proposal or, under
      `NO PROPOSAL`, a `nothing to do on the YYYY-MM-DD close: ...` line with
      each sleeve's reason, for example `short-term: no breakout -- closed
      ... against the prior 20-day high ... and the 100-day average ...;
      long-term: 0 of 10 tranches bought; waits for a close under its 200-day
      average ... (closed ...)`.

Once it has run, replace the invented REST payloads in
`tests/unit/test_robinhood_client.py` with trimmed live captures.

### 5. Watch it

```powershell
.venv\Scripts\rhca status                 # loop RUNNING/NOT RUNNING, both sleeves, keys
.venv\Scripts\rhca audit --kind proposal  # every proposal, with its rule and trigger_reason
```

After each UTC daily close, the loop logs what the split wants, afresh each
hour, for as long as it still wants it:

| Sleeve | Rule | What it proposes |
|---|---|---|
| `short-term` | `entry` | Buy: the close is over the prior 20-day high and the 100-day average. Sized so the stop risks 1% of the sleeve, at most $25; trimmed to the 20% per-coin limit. |
| `short-term` | `stop` | Sell all the sleeve holds of the coin: the close is 3 x ATR(20) under its highest close since entry. |
| `long-term` | `tranche` | Buy one $6.25 tranche: the close is under the 200-day average, a week or more after the last. Never sold. |

Each proposal is either `proposed` (passed all 14 risk rules) or
`rejected_by_risk`. A `proposed` row is still only a proposal. Acting on it is
the normal flow below: a human names the id, `rhca approve` re-checks
everything, and Claude Code places the order through MCP.

`rhca status` shows each sleeve: its cash, what it holds of each coin and
what that cost, the entry close of each short-term position, the tranches
bought, and the realized P&L from the fills you recorded. That is the number
to watch. `rhca accuracy` still reports the retired System 1's six-hour hit
rate, for the record; the split's proposals are not scored there, because a
breakout is held until its stop and a tranche for good.

## First run on a funded account

Before anything touches real money:

```bash
pip install -e ".[dev]" && pytest        # all offline
rhca describe-tools                      # confirm which tools mutate
rhca status                              # should say: no history, switch released
```

Then, in Claude Code with the `RobinHood` MCP server connected:

1. **Resolve the account.** Call `get_accounts`, pipe into
   `rhca ingest accounts --rhs-account-number <the Agentic account's>`.
   Without the flag it caches the first account listed, which is your
   default account, not the one orders go to. Confirm the printed
   `rhs_account_number` is the Agentic account's **numeric** one, and export
   it: `export RHCA_RHS_ACCOUNT_NUMBER=<that value>`.
2. **Confirm the switch works.** `rhca kill-switch on`, then try
   `rhca approve` on anything — it must refuse. `rhca kill-switch off`.
3. **Check the contract guard.** Feed `rhca validate-order` a payload with the
   *alphanumeric* `account_number` and confirm it is rejected.

## Daily loop

### Ingest
```bash
# get_currency_pairs  -> increments, halts, market-only flags
rhca ingest pairs     -f pairs.json
# get_portfolio (Agentic account_number)
#                     -> account value (concentration limit) + crypto buying power
rhca ingest portfolio -f portfolio.json
# get_crypto_positions (Agentic rhs_account_number)
#                     -> enables sell coverage and the open-position cap
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
# re-fetch get_crypto_positions and get_portfolio -> rhca ingest positions / portfolio
```

A fill changes the Agentic account's holdings and buying power, and neither
`rhca run` nor `analyze` sees that until you ingest them again.

`place_crypto_order` usually answers before the order fills: state `new`, 0
filled. Record that response anyway. It logs the order and adds nothing to the
filled quantity. Once it fills, fetch it with `get_crypto_orders`, passing the
`order_id` it returned, and record that response against the same tranche.
Until you do, the audit log says nothing filled. The daily cap then
undercounts, and `rhca approve` would let the same proposal be sent again.
Record each fill once: two records of one filled order count it twice.

Every limit price is rounded to the pair's tick, from the cached pairs, before
it goes out: down for a buy, up for a sell. Robinhood refuses a price off the
tick ("round your order price to the nearest cent"). If `plan-order` warns
that no tick is cached, pipe `get_currency_pairs` into `rhca ingest pairs`
first.

For a `STAGED` plan, repeat `approve` / `place` / `record-execution` per
tranche, incrementing `--tranche`. An unfilled tranche is expected — do not
chase it.

### The split's stops

A `stop` proposal is a `proposed` sell like any other on the dashboard. Take
it through the same steps: `plan-order`, preview, `approve` by its id, place,
`record-execution`, and record the fill. Then re-ingest positions and
portfolio. It sells only what the short-term sleeve holds; the long-term
sleeve's tranches of the same coin stay.

The split knows what each sleeve holds only from the fills you record. An
unrecorded buy is never sold, and an unrecorded sell leaves the sleeve
thinking it still holds the coin. An order recorded but not yet filled or
canceled holds its place -- no second entry, stop or tranche while it works --
and an open buy's dollars stay out of the sleeve's cash. Record the final
state from `get_crypto_orders` either way. If a sell is blocked by
`sell_coverage`, the holdings snapshot predates the buy: re-ingest positions.

The rule decides once a day. A proposal approved hours after the close may
have drifted past the tolerance: `rhca analyze` makes a fresh one at the
current quote, for the same decision.

### Close the day
```bash
# call get_realized_pnl
rhca record-pnl -f realized_pnl.json
rhca status
```

## Backtesting

Before a rule trades real money, replay it over history. Run this on the PC;
the cloud sandbox cannot reach Coinbase:

```powershell
.venv\Scripts\rhca backtest --days 180
.venv\Scripts\rhca backtest --days 365
.venv\Scripts\rhca backtest --days 730 --roll-window 90       # plus rolling windows
.venv\Scripts\rhca backtest --days 730 --roll-window 90 --roll-step 15
.venv\Scripts\rhca backtest --ladder 5:5,10:10,20:20,40:40     # try other steps
```

The symbols, the steps and the trend window default to the watchlist and
`config/strategy.yaml` (BTC, ETH, SOL and XRP, `5:5,10:10,20:20`, 50 days). The 50 days
of warm-up for the average are fetched before the window, so the window
itself is exactly `--days` long. `--trend-days 0` drops every trend row.

It compares, on the same bars:

- **ladder (lot)** and **ladder (anchor)**: buy $5, $10 and $20 at 5%, 10%
  and 20% under the anchor. `lot` sells each lot at its own step over where it
  was bought. `anchor` sells from the anchor: $5 worth at +5%, $10 worth at
  +10%, everything left at +20%.
- **… +50d**: the same, buying only on a close above the 50-day average.
- **… +50d exit**: that, and selling everything on a close at or under it.
  This was the rule the agent traded until 2026-09-28; it now trades the split.
- **trend +50d**: $100 held while the close is above the average, nothing at
  or under it.
- **hold**: $100 bought on the first bar.

Every trade pays the 1.9% round trip, and `stressed` re-runs at 2.85%. Bars
are cached for 6 hours under `data/backtest/`; `--refresh` refetches.

**Read it in this order:** total P&L and `on capital`, worst drawdown, then
`win +open`. `win closed` counts only trades that were sold. A ladder sells
only into strength, so in a falling market it closes almost nothing and its
closed win rate stays perfect while `open P&L` holds the loss. The ladders
tie up at most $35 per coin and the baselines $100, so compare `on capital`.
Check `trades` too: the exit rows can churn when the price hovers near the
average (`strategy.md`, "Watch the trades column").

`--roll-window 90` re-runs everything over 90-day windows, a new one every
`--roll-step` days (default 30), each starting flat. It shows, per strategy:
how many windows made money, how many beat `hold` (normal and stressed), and
the median, worst and best return on capital. It shows whether a result
holds across start dates or rests on one lucky one.

**The bar the agent's rule has to clear** before its first live proposal is
approved is in [`strategy.md`](./strategy.md#validating-it).

**The breakout** (`strategy.md`, "The breakout candidate") runs by default
as one account across every symbol, after the per-coin report. To test it on
the four coins it was designed for:

```powershell
.venv\Scripts\rhca backtest --symbols BTC-USD,ETH-USD,SOL-USD,XRP-USD --days 730 --roll-window 90
```

Read its expectancy (mean R per closed trade, after costs) first, then its
max drawdown against hold's. Its bar is in `strategy.md`, "The bar it has to
clear". `--strategies breakout` runs it alone, without the ladder's tables. A backtest
authorizes nothing. It doesn't change a limit or the approval gate, and it
says nothing about the next trade.

**The split** (`strategy.md`, "The split") -- the rule the agent trades --
runs part of the account long-term and the rest in the breakout: by default
the forward test's split (`config/shadow.yaml`: 50% long-term), with no coin
over the per-coin limit in `config/risk_limits.yaml` (20%):

```powershell
.venv\Scripts\rhca backtest --days 1460 --roll-window 90 --strategies split
```

It shows the long-term sleeve bought three ways (buy low, weekly DCA, lump
sum), the breakout sleeve, the split, and the whole account in the breakout
or in hold, and how often the per-coin limit trimmed or turned away a buy.
`--long-mode`, `--long-pct` and `--long-symbols` change the long-term sleeve;
`--coin-cap-pct` the limit.

## Forward test (`rhca shadow`)

`config/shadow.yaml` turns it on (it ships on). While `rhca run` is running,
it records the split's paper account after every UTC daily close: one
`shadow_day` record in the audit log, and a line in the run's output:

```
forward test (paper, not a proposal) 2026-10-02 close: short-term buy SOL-USD $24.10 at 143.2
forward test (paper) 2026-10-02 close: 1 paper fill(s); split account $501.30 after 5 close(s)
```

`rhca status` shows the paper account in two lines. For the full report --
every paper fill, what is held, and the three checks of its bar:

```powershell
.venv\Scripts\rhca shadow
```

The bar is read after 90 daily closes, on 2026-12-27 (`strategy.md`, "The
forward test"). Until then every verdict says "so far". Keep `rhca run`
running: a day it was not running is not compared, only replayed.

**A paper fill is not a proposal.** It has no proposal id, and nothing here
trades it. If `rhca shadow` shows a buy, that is the paper account's, not a
trade to make.

Changing `config/shadow.yaml` restarts the test; delete it to turn the test
off.

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
