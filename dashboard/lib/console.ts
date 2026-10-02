/**
 * Everything a console page reads, loaded once per request.
 *
 * The shell and the page both call this; `cache` makes the second call free,
 * so a page never pays for two KV round trips.
 */

import { cache } from "react";

import { type Fated, withFates } from "./flow";
import { canDecide, isAuthenticated, passwordConfigured } from "./session";
import { getDecisions, getPayload, storageMode } from "./store";

export const FALLBACK_REPO = "https://github.com/joman124/robinhood-crypto-agent";

export const loadConsole = cache(async () => {
  const [payload, decisions, authed, mayDecide] = await Promise.all([
    getPayload(),
    getDecisions(),
    isAuthenticated(),
    canDecide(),
  ]);
  const rows: Fated[] = withFates(payload?.proposals ?? [], decisions);
  return {
    payload,
    decisions,
    authed,
    mayDecide,
    rows,
    passwordSet: passwordConfigured(),
    // Password set but not signed in: render only the sign-in form, never the data.
    gated: passwordConfigured() && !authed,
    storage: storageMode(),
    repo: payload?.repo_url || process.env.NEXT_PUBLIC_REPO_URL || FALLBACK_REPO,
  };
});

export type Console = Awaited<ReturnType<typeof loadConsole>>;
