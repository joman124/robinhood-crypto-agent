// Self-check for lib/flow.ts. Run: node lib/flow.check.mjs (Node 22.18+ strips the types).
import assert from "node:assert/strict";

import { fateOf, flowLinks, rateOf, routeOf, tally, withFates } from "./flow.ts";

const base = { symbol: "BTC-USD", side: "buy", actionable: true, risk_passed: true, outcome: null };
const p = (id, extra = {}) => ({ ...base, proposal_id: id, ...extra });
const accept = { proposal_id: "a", decision: "accept" };

assert.equal(fateOf(p("x", { execution: { tranches: 1 } }), undefined), "placed");
assert.equal(fateOf(p("a"), accept), "accepted");
assert.equal(fateOf(p("d"), { decision: "decline" }), "declined");
assert.equal(fateOf(p("b", { risk_passed: false, actionable: false }), undefined), "blocked");
assert.equal(fateOf(p("h", { actionable: false, status: "not_escalated" }), undefined), "held");
assert.equal(fateOf(p("w"), undefined), "awaiting");

assert.deepEqual(routeOf("placed"), ["all", "cleared", "accepted", "placed"]);
assert.deepEqual(routeOf("blocked"), ["all", "blocked"]);

const rows = withFates(
  [
    p("1", { strategy: "split", sleeve: "long-term", execution: { tranches: 1 } }),
    p("2", { strategy: "split", sleeve: "long-term" }),
    p("3", { strategy: "split", sleeve: "short-term", risk_passed: false, actionable: false }),
    p("4", { outcome: { verdict: "win" }, actionable: false, status: "not_escalated" }),
  ],
  [{ proposal_id: "2", decision: "decline" }],
);
const { nodes, links } = flowLinks(rows);
assert.equal(nodes.get("all"), 4);
assert.equal(nodes.get("cleared") + nodes.get("blocked"), 4, "every proposal takes exactly one risk branch");
assert.equal(links.get("accepted>placed"), 1);

const bySegment = tally(rows, (r) => r.segment);
assert.deepEqual(rateOf("accepted", bySegment.get("long")), { hits: 1, of: 2, rate: 0.5 });
assert.deepEqual(rateOf("cleared", bySegment.get("short")), { hits: 0, of: 1, rate: 0 });
assert.equal(rateOf("won", bySegment.get("long")).rate, null, "no scored outcome is unknown, not 0%");
assert.equal(rateOf("won", bySegment.get("system1")).rate, 1);

console.log("flow.check: ok");
