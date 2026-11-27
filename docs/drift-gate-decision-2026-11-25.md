# Decision sheet: the drift gate compares an ask with a mid

For task t285. This is read-only: no order was placed, no RobinHood tool was called, and no rule, limit or line of code was changed. It builds on the finding in PR #43 (`docs/stale-live-proposals-2026-11-24.md` §3), and it feeds **Decision 3** of the verdict sheet (`docs/forward-test-verdict-2026-12-26.md`), which is due **2026-12-27**.

Sources: `src/` on `main` at `88bf123`, `data/audit_log.jsonl` through the 2026-11-25 05:00 UTC cycle, and the cached quotes in `data/market_state.json` (observed 2026-11-25 05:31 UTC).

**In one line:** the gate refuses every fresh buy on all four coins, because Robinhood's spread alone (about 1.9%) reads as about 0.93% of "drift" against a 0.5% tolerance. **Recommendation: Option B** (measure a buy against the ask and a sell against the bid). Ship it together with PR #43's fix that gives `rhca approve` the order desk's superseded and age checks. Do this before the desk is relied on for Decision 3.

## 1. How the check works today

| Step | Code | Price used |
|---|---|---|
| A split buy is logged | `agent.py` `_proposal`: `reference = quote.ask` | the **ask** at logging |
| A split sell is logged | same: `reference = quote.bid` | the **bid** at logging |
| Its limit order | `orders.single_order_plan` / `build_order_request` | the reference, snapped, as a `gtc` limit |
| The drift check at accept | `execution/gate.py` `_check_drift`: `abs_pct_drift(live_quote.mark, proposal.reference_price)` | the live **mark**, which is the mid |
| Tolerance | `config/risk_limits.yaml` `price_drift_tolerance_pct` | **0.5%** (ceiling 1.5%) |
| The dashboard's warning | `dashboard._drift` + `dashboard/lib/format.ts` `acceptBlocker` | the last seen **mark**, the same measure |

The order desk (`execution/desk.py`) and `rhca approve` both reach `_check_drift` through `ApprovalGate`. The drift is `|mid_now − ask_then| / ask_then`. At the moment a buy is logged, with spread *s*% of the mid, that is

  drift = (s/2) / (1 + s/200)

So the check passes a quote against itself only when the spread is under about **1.005%**.

## 2. The spreads the four coins actually show

| Coin | Live proposals logged | Spread at logging, min / median / max | Spread now (05:31 UTC) | Drift the gate reads on a fresh buy now |
|---|---|---|---|---|
| BTC | 280 | 1.850% / 1.885% / 1.906% | 1.878% | 0.930% |
| ETH | 302 | 1.843% / 1.885% / 1.902% | 1.882% | 0.932% |
| SOL | 302 | 1.838% / 1.887% / 1.924% | 1.886% | 0.934% |
| XRP | 0 (the loop logs no XRP tranche; verdict sheet Decision 4) | none recorded | 1.898% | 0.940% |

All 884 live proposals (2026-11-12 02:49 to 2026-11-25 05:00 UTC) are long-term buys, every one `proposed`, and none has ever been accepted. The narrowest spread recorded on any of them is 1.838%, still nearly double the 1.005% at which a fresh buy could pass. **None of the 884 would have passed the drift check against the quote it was logged on.**

## 3. Which approvals the gate refuses even when fresh

- **Every fresh buy, on all four coins.** A buy passes only when the mid has risen by between about **0.44% and 1.45%** since it was logged (at a 1.89% spread). That rise has to happen inside the 75 minutes before the desk calls the proposal stale.
- **Every fresh sell, by the same arithmetic.** A sell is measured from the bid, so it passes only when the mid has fallen by about 0.45% to 1.44%. No live sell exists today (the breakout sleeve has made no trades). But a breakout stop-loss would be refused when the price is flat or bouncing, which is exactly when an exit is wanted.
- **The accepts that do pass send a limit order that does not fill at once.** When a buy passes, the price has risen, so the current ask sits above the proposal's ask. The limit sits inside the spread and stays open as a `gtc` order until it fills or is cancelled in the Robinhood app (CLAUDE.md, "Following an execution plan").
- **Stale proposals pass instead.** At 05:31 UTC, 15 old ids pass the drift check. Only `rhca approve` would take one of them, because the order desk refuses any id that is superseded or over 75 minutes old.

Live example, the three newest proposals (logged 2026-11-25 05:00 UTC), measured against the 05:31 quote:

| Proposal | Coin | Drift as gated (vs mid) | Drift ask-to-ask |
|---|---|---|---|
| `d500334aa473` | BTC | 1.217% (refused) | 0.289% |
| `94f4e327e419` | ETH | 1.227% (refused) | 0.297% |
| `52d1024cbb5e` | SOL | 1.405% (refused) | 0.475% |

These figures shift with every quote. §6 shows how to re-run them.

This also undoes the remedy in CLAUDE.md rule 4 ("re-run `rhca analyze` for a fresh proposal"). A fresh proposal is priced at the ask of the quote it came from, so the gate refuses it too, at about 0.93%. Re-running never converges.

## 4. The options

None of them widens the tolerance. It stays at 0.5% in all three.

### Option A: leave it

- **What changes:** nothing.
- **Safety:** it fails closed for current proposals. In practice no dashboard accept buys anything unless the price happened to rise 0.44% to 1.45% within the hour. When one does pass, it rests as a limit order that does not fill at once. The cost: Decision 3's "start accepting tranches" turns into "keep declining" without anyone deciding that, and every refusal is pushed to the owner as noise. A breakout stop-loss would be refused when it is needed, though the sleeve holds nothing today. `rhca approve` keeps passing stale ids (15 now).
- **Retest:** none. Do record the outcome, so that Decision 3 is not read as "accepting will work".

### Option B: measure drift against the same side (ask-to-ask for a buy, bid-to-bid for a sell). Recommended.

- **What changes:** `_check_drift` compares the live **ask** with a buy's reference, and the live **bid** with a sell's. The dashboard's `_drift` and `acceptBlocker` use the same measure, so the page and the desk agree. Today the payload carries only the last mark, so the latest ask and bid have to be added to it.
- **Safety:** the check then measures what it claims to measure: how far the price you would pay now has moved from the price that was approved. The tolerance, the limit price and the order type stay the same. If the ask has fallen, the limit is marketable and fills at or below the approved price. If it has risen by up to 0.5%, the order rests. Two side effects:
  1. More stale ids clear drift (34 rather than 15 at 05:31 UTC). Only `rhca approve` can act on them, so ship this **together with** PR #43's candidate fix that gives `cmd_approve` the desk's superseded and age checks.
  2. Measured from the mid, this lets through quotes that today's gate reads as over 0.5%. That is the spread offset being removed, not a wider tolerance. The owner should confirm that reading, because the sector rule forbids widening.
- **Retest:**
  - `tests/unit/test_orders_and_gate.py`, the drift cases. The shared `quote` fixture in `tests/conftest.py` has a 0.25% spread (79900/80100), which is why this bug never showed. Add cases at a real ~1.9% spread: a fresh buy passes, a buy whose ask moved 0.6% is refused, and the same pair for a sell against the bid.
  - `tests/unit/test_desk.py::test_a_price_that_moved_past_the_drift_tolerance`. It moves only `broker.mark`, so it must move the ask (and the bid, for a sell).
  - `tests/integration/test_cli_workflow.py::test_price_drift_refuses_a_stale_proposal` and its `moved_quotes.json` fixture.
  - The dashboard's `_drift`, the `acceptBlocker` copy, and `tests/unit/test_dashboard_payload.py`.
  - The `cmd_approve` superseded and age refusal, with its own tests.
  - Then the full suite, and a read-only run of §6 against the live log to confirm that the newest three proposals clear. Only after that should the owner try a real accept.

### Option C: re-quote at approval

- **What changes:** at accept, the desk takes the fresh quote and re-prices the order at the fresh ask (or bid). It re-sizes the $6.25 into a new quantity, re-runs the risk engine on that quote, and records both the approved and the sent price. Drift becomes the gap between the fresh ask and the approved ask. So Option C contains Option B and adds the re-pricing.
- **Safety:** the order is marketable and fills straight away at the current ask, with no resting limit orders. But the order sent is no longer the one the owner approved. Price and quantity differ by up to 0.5%. That breaks two written rules: "never re-price it to chase a fill" (CLAUDE.md, "Following an execution plan") and "send that payload verbatim" (rule 2). It needs a fresh risk pass (spread, quote age) on every send. It is the largest change to the one code path that places real orders.
- **Retest:** everything under Option B, plus:
  - the desk's re-sizing and re-pricing;
  - the risk re-evaluation on the fresh quote, including refusals;
  - audit records for an approved-versus-sent price, and the ledger and daily-cap sums built from them;
  - an idempotency key that stays the same across a re-price;
  - a dashboard preview of the price that will actually be sent;
  - the CLAUDE.md rule changes, approved by the owner.

## 5. What the owner needs to decide, and when

1. **Before 2026-12-27:** A, B or C. If Decision 3 is going to be "start accepting tranches", then A makes that decision a no-op, and B (or C) has to be merged and retested first.
2. **If B:** confirm that measuring a buy against the ask is a correction and not a widening of the tolerance, and that the `cmd_approve` stale-id fix ships alongside it.
3. **Either way:** until a fix is merged, an accept on the dashboard will mostly come back "price has moved". That is the gate working as built, not a fault to route around. Do not retry with a wider tolerance, and do not use `rhca approve` on an older id.

## 6. Reproducing the figures

Read-only. Run from the repo root. The quote moves every cycle, so the counts will too.

```python
import json
from decimal import Decimal as D
log = [json.loads(l) for l in open("data/audit_log.jsonl", encoding="utf-8") if l.strip()]
live = [r for r in log if r["kind"] == "proposal" and r.get("mode") == "live"]
q = json.load(open("data/market_state.json", encoding="utf-8"))["quotes"]["value"]
pct = lambda new, old: abs(new - old) / old * 100
for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"):
    b, a, m = D(q[s]["bid"]), D(q[s]["ask"]), D(q[s]["mark"])
    print(s, "spread%", round((a - b) / m * 100, 3), "fresh-buy drift%", round(pct(m, a), 3))
newest = {r["symbol"]: r for r in live}
for s, r in newest.items():
    ref = D(r["reference_price"])
    print(r["proposal_id"], s, "vs mid", round(pct(D(q[s]["mark"]), ref), 3),
          "ask-to-ask", round(pct(D(q[s]["ask"]), ref), 3))
tol = D("0.5")
print("pass vs mid:", sum(pct(D(q[r["symbol"]]["mark"]), D(r["reference_price"])) <= tol for r in live),
      "pass ask-to-ask:", sum(pct(D(q[r["symbol"]]["ask"]), D(r["reference_price"])) <= tol for r in live))
```
