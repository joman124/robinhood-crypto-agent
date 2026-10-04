// Self-check for lib/pnl.ts. Run: node lib/pnl.check.mjs (Node 22.18+ strips the types).
import assert from "node:assert/strict";

import { pnlSummary, signedMoney, takenTrades } from "./pnl.ts";

const fill = (qty) => ({ tranches: 1, filled_quantity: String(qty), state: "filled", overridden: false });
const p = (id, extra) => ({ proposal_id: id, symbol: "SOL-USD", side: "buy", reference_price: "100", notional: "50", actionable: true, outcome: null, ...extra });

const payload = {
  proposals: [
    p("stop", { side: "sell", sleeve: "short-term", proposed_at: "2026-10-03", execution: fill(0.5) }),
    p("entry", { sleeve: "short-term", proposed_at: "2026-10-01", execution: fill(0.5) }),
    p("tranche", { sleeve: "long-term", proposed_at: "2026-10-01", execution: fill(0.25) }),
    p("unplaced", { sleeve: "long-term", proposed_at: "2026-10-02" }),
    p("btc", { symbol: "BTC-USD", sleeve: "long-term", proposed_at: "2026-10-02", execution: fill(0.001) }),
  ],
  market: { "SOL-USD": { mark: "120", observed_at: "x" } },
  split_realized_pnl: { "SOL-USD": "-4.50", "BTC-USD": "10" },
  today: { executions: 1, executed_notional: "50", realized_pnl: "-4.50", proposals: 1 },
};

const byId = Object.fromEntries(takenTrades(payload).map((t) => [t.p.proposal_id, t]));
assert.equal(byId.unplaced, undefined, "only proposals with a recorded fill are trades you took");
assert.equal(byId.entry.open, false, "a later recorded stop closes the breakout entry");
assert.equal(byId.stop.open, false, "a sell is never an open position");
assert.equal(byId.tranche.open, true, "a long-term tranche is never sold");
assert.equal(byId.tranche.move, 5, "0.25 × (120 − 100)");
assert.equal(byId.btc.move, null, "no mark means no estimate, not zero");

const s = pnlSummary(payload);
assert.equal(s.realized, 5.5);
assert.deepEqual(s.bySymbol.map(([sym]) => sym), ["BTC-USD", "SOL-USD"], "best coin first");
assert.equal(s.open, 5);
assert.equal(s.today, -4.5);
assert.equal(pnlSummary({ proposals: [] }).realized, null, "no fills is unknown, never $0");
assert.equal(signedMoney(-3.05), "−$3.05");
assert.equal(signedMoney(12.4), "+$12.40");

console.log("pnl.check: ok");
