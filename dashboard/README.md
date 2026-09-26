# Dashboard

A web view of every trade the agent has suggested, whether those suggestions
turned out to be good, and a place to accept or decline the ones waiting on you.

## What is on the page

- **Status strip.** Whether the shadow loop (`rhca run`) is running, the kill
  switch, the execution mode, and how old the last sync is. Banners explain
  anything that changes what the numbers mean: an engaged kill switch, a stale
  sync, read-only mode, in-memory storage.
- **Headline numbers.** Hit rate, average move, proposal count, and today's
  proposals, executions, traded dollars and realized P&L.
- **The proposal log.** "Needs your call" by default, with Decided and All tabs,
  search, stage/pair/outcome filters, pagination, CSV export, and a card layout
  on phones. Accept is greyed out, with the reason, whenever the agent's gate
  would refuse it: risk-blocked, a record rather than a proposal, the kill
  switch, or price drift past `price_drift_tolerance_pct` as of the last sync.
- **The detail drawer** (click any row; `#p=<id>` links straight to one). The
  decision with an optional note and whether the agent has picked it up; the
  trade, plan and drift; trigger and System 2 verdicts; every signal on a
  −1…+1 track with its weight and rationale; all 17 risk rules; the outcome;
  any execution; and the `rhca` commands to copy into the terminal.
- **Hit rate over time**, the **shadow loop** heartbeat (counts, services, last
  error task), and **accuracy by** pipeline stage, regime, pair or side.

The page re-fetches every minute while the tab is visible.

## What it does and does not do

**It does not place orders, and it holds no Robinhood credentials.**

Accepting a proposal here *records your decision*. The agent picks that decision
up on its next `rhca dashboard-sync` and still runs it through the full approval
gate before anything is submitted: the kill switch, a fresh price-drift check
against the live quote, remaining-quantity accounting across tranches, and
order-contract validation. A web button is an input to that gate, never a
bypass of it.

Two consequences worth stating plainly:

- **A risk-blocked proposal has no Accept button.** Overriding the risk engine
  stays a deliberate, typed act in the terminal. The phrase that does it is
  redacted from anything you type here, *and* the agent refuses an override
  that did not come from a typed instruction — two independent guards.
- **The data pushed here is deliberately thin.** No account numbers, no buying
  power, no portfolio value, no order ids. The dashboard needs to know what was
  suggested and how it turned out; it does not need to know how much money sits
  behind it. Risk rules whose message would quote the account (sizing, the
  dollar caps, open positions, concentration, sell coverage) arrive as a
  pass/fail verdict with no text, System 2's written rationale never arrives,
  and neither does the text of the loop's last error.

## Deploying to Vercel

```bash
# 1. From the repository root
cd dashboard
npx vercel            # link the project, first deploy
```

If the Vercel project is connected to the GitHub repo instead, set **Project →
Settings → Build and Deployment → Root Directory** to `dashboard`. Left at the
repository root, Vercel sees `pyproject.toml`, builds a Python app, and fails
with "No python entrypoint found".

Then set these in **Project → Settings → Environment Variables**:

| Variable | Required | What it is |
|---|---|---|
| `RHCA_DASHBOARD_TOKEN` | yes | Shared secret for `rhca dashboard-sync`. `openssl rand -hex 32` |
| `DASHBOARD_PASSWORD` | to decide | Password for accepting/declining. **Unset ⇒ read-only** |
| `NEXT_PUBLIC_REPO_URL` | no | Repo link in the header |
| `KV_REST_API_URL` / `KV_REST_API_TOKEN` | in production | Set automatically by the Vercel KV integration |

Add **Storage → KV** from the Vercel dashboard before relying on it. Without
it the app falls back to in-process memory, which does not survive a cold start
and is not shared between serverless instances — the page says so in a banner
rather than pretending otherwise.

Consider also turning on **Deployment Protection** so the whole deployment sits
behind Vercel auth. It composes with the password.

Then, where the agent runs:

```bash
export RHCA_DASHBOARD_URL=https://your-project.vercel.app
export RHCA_DASHBOARD_TOKEN=<the same token>
rhca dashboard-sync
```

That one command pushes proposals and outcomes up, and pulls your decisions
back down. Run it on a schedule alongside your quote ingestion.

## Keeping dependencies deployable

Vercel **refuses to deploy a Next.js version with a known CVE**, so a stale
dependency shows up as a blocked production deploy rather than a warning. CI
runs `npm audit --audit-level=high` on this package to catch it a step earlier.

When it fires:

```bash
cd dashboard
npm audit                      # read the advisory and the fixed version
npm install next@<fixed>       # for a direct dependency
```

For a **transitive** dependency, add an `overrides` entry instead — Next pins
its own copies of `postcss` and `sharp`, and overriding them is how they get
patched without waiting for a Next release:

```json
"overrides": { "postcss": "^8.5.23", "sharp": "^0.35.4" }
```

Re-run `npm run build` after any override: pinning a transitive to a version
its parent did not expect is exactly the kind of change that compiles in
theory and breaks in practice.

## Running locally

```bash
npm install
RHCA_DASHBOARD_TOKEN=dev-token DASHBOARD_PASSWORD=dev npm run dev
# then, from the repo root:
rhca dashboard-sync --url http://localhost:3000 --token dev-token
```

`http://localhost:...` is the one non-HTTPS URL the agent will send a token to.

## Reading the numbers

- **Hit rate** is wins as a share of wins + losses. Flat outcomes — made money,
  but no more than the hurdle — are excluded, and a proposal still inside its
  horizon is never counted as a loss. With nothing resolved the rate shows as
  `—`, meaning *unknown*, not zero.
- An outcome is scored a fixed number of bars after the proposal, as the whole
  round trip: in at the proposal's price (the ask, for a buy), out at the far
  side of the book (the bid). It is a win only if it made more than the hurdle
  after that, and a loss if it lost money at all. A move that merely beat the
  spread on the mark is not a profit in practice, so it is not a win here.
- A time exit is a rule closing a position, not a prediction, so it is not
  scored.
- Outcomes are scored for **every** proposal, including ones you declined. The
  record is of the agent's judgement, not of the subset you happened to like.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `/` | session cookie | The dashboard |
| `POST /api/proposals` | bearer token | Ingest from `rhca dashboard-sync` |
| `GET /api/proposals` | bearer token | Read back the stored payload |
| `GET /api/decisions` | bearer token | Decisions for the agent to pull |

Decisions are **written** only by a server action behind the session cookie —
there is no HTTP route that accepts a decision, so there is nowhere to POST a
forged one.
