// Self-check for lib/session.ts. Run: node lib/session.check.mjs (Node 22.18+ strips the types).
import assert from "node:assert/strict";

import { SESSION_MS, sessionValid, sessionValue } from "./session.ts";

const now = Date.parse("2026-11-02T12:00:00Z");
const fresh = sessionValue("pw", now);

assert.equal(sessionValid("pw", fresh, now), true);
assert.equal(sessionValid("pw", fresh, now + SESSION_MS - 1), true);
// A copied cookie stops working at 12 hours, whatever the browser does with maxAge.
assert.equal(sessionValid("pw", fresh, now + SESSION_MS + 1), false);
// Re-dating it breaks the signature.
const [, mac] = fresh.split(".");
assert.equal(sessionValid("pw", `${now + SESSION_MS}.${mac}`, now + SESSION_MS + 1), false);
// Another password, junk, a future stamp, an unset password: all refused.
assert.equal(sessionValid("other", fresh, now), false);
assert.equal(sessionValid("pw", "junk", now), false);
assert.equal(sessionValid("pw", "", now), false);
assert.equal(sessionValid("pw", sessionValue("pw", now + 60_000), now), false);
assert.equal(sessionValid("", fresh, now), false);

console.log("session.check: ok");
