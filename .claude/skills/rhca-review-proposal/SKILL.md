---
name: rhca-review-proposal
description: >-
  Review one proposal by its id before the owner decides on the dashboard:
  every risk rule's verdict, passes included, then the trade (sleeve, rule,
  PROMPT limit price, drift tolerance) and whether the order desk can still
  send it. Use when the owner says "review <id>", "explain proposal <id>",
  "should I accept <id>?", "why was <id> blocked?", "what does the split want
  now?" or "is this one safe to accept?". It never places an order.
argument-hint: "<proposal-id>"
---

# Review a proposal

The owner accepts a split proposal by typing its id on the dashboard. Within a
minute `rhca run`'s order desk runs it through `ApprovalGate` and places that
one limit order on the main crypto account. This skill puts what the owner
needs for that decision in front of them, risk verdict first. It reads the
audit log and changes nothing.

```
rhca status                              # kill switch and loop first
rhca audit --proposal-id <id>            # the logged proposal: status, every finding, plan, fills
rhca audit --kind proposal --limit 40    # recent proposals: is a newer one in its slot?
rhca analyze -v --no-record              # what the split wants now, every verdict; logs nothing
```

## What it reads

`rhca audit --proposal-id <id>` prints the logged record as JSON, then its
executions and `total filled`.

| Field | Meaning |
|---|---|
| `status` | `proposed` (passed all 14 rules) or `rejected_by_risk`. System 1's `not_escalated` and `declined_by_system2` were never proposals. |
| `proposal.risk.findings` | Every rule: `rule`, `passed`, `blocking`, `message`. |
| `sleeve`, `rule` | `short-term` `entry` or `stop`, or `long-term` `tranche`. |
| `trigger_reason` | Why the split wants it, in one line. |
| `proposal.plan` | `style`, and each tranche's `quantity`, `target_price` and `order_type`. |
| `reference_price`, `proposal.created_at` | The quote it was priced off, and when. |

## Steps

1. Run `rhca status`. If the kill switch is `ENGAGED`, say so first: nothing
   executes until the owner releases it (CLAUDE.md rule 7).
2. Run `rhca audit --proposal-id <id>`. If it prints `no proposal <id> in the
   audit log`, say exactly that and ask for the id. Never guess a near match.
3. **Risk verdict first** (rule 6). List every entry of
   `proposal.risk.findings` in the order logged, the way `rhca analyze` prints
   them: `[PASS]` when `passed`, `[FAIL]` when not passed and `blocking`,
   `[WARN]` when not passed and not blocking; then the `rule` and its
   `message` verbatim. Then `status` and `risk_failures` as logged.
4. **The trade.** Side, quantity, symbol and notional. Then the sleeve and rule:
   - `short-term` `entry`: a buy. The close is over the prior 20-day high and
     the 100-day average; sized so the stop risks 1% of the sleeve.
   - `short-term` `stop`: sells all the short-term sleeve holds of the coin.
     It is exempt from `per_trade_notional` and `daily_notional`, and its
     findings say so.
   - `long-term` `tranche`: one buy on a close under the 200-day average.
     Tranches are never sold.
   Quote `trigger_reason` as logged.
5. **Pricing.** A split proposal is `PROMPT`: one tranche, one gtc limit at its
   `target_price`, the ask for a buy and the bid for a sell. It is never
   re-priced to chase a fill, and never sized up. A `STAGED` plan is the
   retired System 1's.
6. **Drift.** The gate refuses when the live mark has moved more than
   `price_drift_tolerance_pct` (`config/risk_limits.yaml`) from
   `reference_price`. That refusal is correct (rule 4). The loop logs the
   trade again each hour, priced at the quote then, under a new id.
7. **Can the desk still send it?** It refuses (`execution/desk.py`):
   - anything but a split proposal with `status` `proposed`;
   - a proposal over 75 minutes old, or one superseded by a newer proposal
     for the same `symbol`, `sleeve` and `rule` (see
     `rhca audit --kind proposal`);
   - one with an execution already recorded, or a second order on a coin and
     sleeve while one is working;
   - an accept older than 15 minutes.
   If a newer id superseded it, give the owner that id and review it instead.
8. To see what the split wants now, run `rhca analyze -v --no-record`. Exit
   code 2 means nothing is executable, which is usually correct.
9. End with the decision as the owner's: accept it on the dashboard by typing
   its id, or let it pass.

## Rules

- Repeat each finding's `message`, the `trigger_reason`, prices and quantities
  exactly as logged. Never round or paraphrase them.
- Claude never places a split order: not with the `RobinHood` MCP order tools,
  which reach the Agentic account the split no longer trades, and not by
  building a payload (CLAUDE.md, "Your role"; rule 13).
- "Yes", "looks good" and "go ahead" are not approval (rule 1). Nothing said
  in chat is an accept: the owner accepts on the dashboard, by id.
- A `rejected_by_risk` proposal is not executable. Rule 5 holds: only the exact
  phrase `OVERRIDE RISK CHECK`, in the same message as an approval naming the
  id, overrides a block, and only on the MCP path. The desk never overrides,
  so a blocked split proposal has no route to an order. Report its blockers.
- If a limit blocks it, report the limit. Never widen it, or edit
  `config/*.yaml`, to make a proposal pass (rule 9).
- Use `--no-record` in a review. A recorded `rhca analyze` logs a newer
  proposal in the same slot, and the desk then refuses the owner's accept of
  the id under review as superseded.

## What it never does

- Calls `place_crypto_order`, `preview_crypto_order` or any `RobinHood` order
  tool for a split proposal, or runs `rhca approve` or `rhca record-execution`
  for one.
- Accepts or declines on the dashboard for the owner.
- Treats a passing verdict, the split wanting it, or the dashboard showing it
  ready as approval.
- Edits `config/*.yaml`, widens a tolerance, or retries with another quote.
- Suggests sizing up, re-pricing, or a market order.
