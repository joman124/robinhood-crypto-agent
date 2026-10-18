// Self-check for lib/triggers.ts. Run: node lib/triggers.check.mjs (Node 22.18+ strips the types).
import assert from "node:assert/strict";

import { triggerGap } from "./triggers.ts";

// BTC from a real snapshot: closed 81378.21, breakout over 86507.11 (+6.3%),
// split under 72424.51 (−11.0%). The breakout is the nearer line.
const btc = triggerGap({
  symbol: "BTC-USD",
  close: "81378.21",
  buy_under: "72424.51",
  bought: 0,
  tranches: 10,
  buy_from: null,
  enter_over: "86507.11",
  stop_under: null,
});
assert.equal(btc.breakout.gapPct.toFixed(1), "6.3", "breakout needs +6.3% up");
assert.equal(btc.split.gapPct.toFixed(1), "-11.0", "split needs 11.0% down");
assert.equal(btc.stop, null, "not holding, so no stop line");
assert.equal(btc.nearest.label, "breakout", "the smaller absolute move wins");

// While the breakout holds (enter_over cleared, a stop set), the stop is the
// only live line, so it is the nearest.
const held = triggerGap({
  symbol: "SOL-USD",
  close: "103.3",
  buy_under: null,
  bought: 0,
  tranches: 0,
  buy_from: null,
  enter_over: null,
  stop_under: "98.14",
});
assert.equal(held.breakout, null);
assert.equal(held.nearest.label, "stop", "a held coin fires on its stop");
assert.equal(held.nearest.gapPct.toFixed(1), "-5.0", "98.14 is 5% under 103.3");

// No lines at all (pre-history, or every rule idle): nearest is null, not a throw.
const bare = triggerGap({ symbol: "XRP-USD", close: "1.29", buy_under: null, bought: 0, tranches: 0, buy_from: null, enter_over: null, stop_under: null });
assert.equal(bare.nearest, null, "no trigger line means no gap, never a crash");

// A zero/garbage close must not divide-by-zero into Infinity.
const bad = triggerGap({ symbol: "ETH-USD", close: "0", buy_under: "2000", bought: 0, tranches: 10, buy_from: null, enter_over: "3000", stop_under: null });
assert.equal(bad.nearest, null, "a close of 0 yields no usable gap");

console.log("triggers.check: ok");
