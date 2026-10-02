/**
 * Whether the shadow loop and the sync look alive. Pure, so the server page
 * and the client status strip judge it the same way.
 */

import { secondsSince } from "./format";
import type { Payload } from "./types";

/** A manual `rhca dashboard-sync` has no schedule to be late for, so give it an hour. */
const MANUAL_SYNC_STALE_SECONDS = 3600;
/** `rhca run`'s default push interval, for payloads that predate the field. */
const DEFAULT_SYNC_INTERVAL_SECONDS = 300;

export type Loop = "running" | "stopped" | "unknown" | "never";

/**
 * The page sees the heartbeat only as of the last sync, and `rhca run` syncs
 * every few minutes, not every cycle. So the loop is judged *at sync time*
 * (was its last cycle fresh when it pushed?), and the sync itself is judged
 * against its own interval plus the same grace. Judging the heartbeat against
 * the wall clock would read "not running" for most of every sync interval.
 */
export function health(payload: Payload, now: number): { loop: Loop; syncStale: boolean; interval: number | null } {
  const beat = payload.pipeline;
  const grace = beat?.stale_after_seconds ?? 180;
  const loopSyncs = Boolean(beat?.services?.dashboard);
  const interval = loopSyncs ? beat?.sync_interval_seconds ?? DEFAULT_SYNC_INTERVAL_SECONDS : null;

  const syncAge = secondsSince(payload.generated_at, now) ?? Infinity;
  const syncStale = syncAge > (interval !== null ? interval + grace : MANUAL_SYNC_STALE_SECONDS);

  if (!beat) return { loop: "never", syncStale, interval };
  const beatAtSync = secondsSince(beat.last_cycle_at, Date.parse(payload.generated_at));
  if (beatAtSync === null || beatAtSync > grace) return { loop: "stopped", syncStale, interval };
  // It was alive when it last pushed; if it pushes on a schedule and has
  // missed it, we no longer know.
  return { loop: loopSyncs && syncStale ? "unknown" : "running", syncStale, interval };
}
