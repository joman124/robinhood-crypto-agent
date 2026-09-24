"use client";

import { RelTime, useNow } from "@/components/Live";
import { secondsSince } from "@/lib/format";
import type { Payload } from "@/lib/types";

/** A manual `rhca dashboard-sync` has no schedule to be late for, so give it an hour. */
const MANUAL_SYNC_STALE_SECONDS = 3600;
/** `rhca run`'s default push interval, for payloads that predate the field. */
const DEFAULT_SYNC_INTERVAL_SECONDS = 300;

type Loop = "running" | "stopped" | "unknown" | "never";

/**
 * The page sees the heartbeat only as of the last sync, and `rhca run` syncs
 * every few minutes, not every cycle. So the loop is judged *at sync time*
 * (was its last cycle fresh when it pushed?), and the sync itself is judged
 * against its own interval plus the same grace. Judging the heartbeat against
 * the wall clock would read "not running" for most of every sync interval.
 */
function health(payload: Payload, now: number): { loop: Loop; syncStale: boolean; interval: number | null } {
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

type Tone = "good" | "warn" | "critical" | "neutral";

function Pill({ tone, label, children }: { tone: Tone; label: string; children: React.ReactNode }) {
  const glyph = { good: "●", warn: "▲", critical: "■", neutral: "○" }[tone];
  return (
    <div className={`pill ${tone}`}>
      <span className="pill-glyph" aria-hidden="true">
        {glyph}
      </span>
      <span className="pill-label">{label}</span>
      <span className="pill-value">{children}</span>
    </div>
  );
}

const LOOP_FACE: Record<Loop, { tone: Tone; text: string }> = {
  running: { tone: "good", text: "running" },
  stopped: { tone: "warn", text: "not running" },
  unknown: { tone: "warn", text: "no word" },
  never: { tone: "neutral", text: "never run" },
};

export function StatusStrip({ payload }: { payload: Payload | null }) {
  const { now } = useNow();

  if (!payload) {
    return (
      <div className="strip">
        <Pill tone="neutral" label="Sync">
          never
        </Pill>
      </div>
    );
  }

  const { loop, syncStale } = health(payload, now);
  const face = LOOP_FACE[loop];
  const kill = payload.kill_switch;

  return (
    <div className="strip" aria-label="Agent status">
      {payload.pipeline !== undefined && (
        <Pill tone={face.tone} label="Shadow loop">
          {face.text}
          {payload.pipeline && (
            <>
              {" "}· last cycle <RelTime iso={payload.pipeline.last_cycle_at} />
            </>
          )}
        </Pill>
      )}
      {kill && (
        <Pill tone={kill.engaged ? "critical" : "good"} label="Kill switch">
          {kill.engaged ? "ENGAGED" : "released"}
        </Pill>
      )}
      <Pill tone={payload.execution_mode === "propose_only" ? "neutral" : "warn"} label="Mode">
        {payload.execution_mode.replace(/_/g, "-")}
      </Pill>
      <Pill tone={syncStale ? "warn" : "neutral"} label="Last sync">
        <RelTime iso={payload.generated_at} />
      </Pill>
    </div>
  );
}

/** Banners for states that change what the page's numbers mean. */
export function StatusAlerts({ payload }: { payload: Payload | null }) {
  const { now } = useNow();
  if (!payload) return null;

  const { loop, syncStale, interval } = health(payload, now);
  const kill = payload.kill_switch;

  return (
    <>
      {kill?.engaged && (
        <div className="alert critical" role="alert">
          <strong>Kill switch engaged</strong>
          {kill.reason ? ` (${kill.reason.replace(/_/g, " ")})` : ""}
          {kill.engaged_at ? (
            <>
              {" "}
              since <RelTime iso={kill.engaged_at} />
            </>
          ) : null}
          . Every execution is refused until a human releases it with{" "}
          <code>rhca kill-switch off</code>.
        </div>
      )}
      {syncStale && (
        <div className="alert warn">
          <strong>Out of date.</strong> The last sync was <RelTime iso={payload.generated_at} />
          , so proposals, prices and drift below are as of then.{" "}
          {interval !== null ? (
            <>
              The shadow loop pushes every {Math.round(interval / 60)} min, so it has probably
              stopped: check <code>rhca status</code>.
            </>
          ) : (
            <>
              Run <code>rhca dashboard-sync</code>, or set <code>RHCA_DASHBOARD_URL</code> and{" "}
              <code>RHCA_DASHBOARD_TOKEN</code> in the agent&apos;s <code>.env</code> so{" "}
              <code>rhca run</code> pushes on a schedule.
            </>
          )}
        </div>
      )}
      {!syncStale && loop === "stopped" && (
        <div className="alert warn">
          <strong>The shadow loop was not running</strong> at the last sync: its last cycle was{" "}
          <RelTime iso={payload.pipeline?.last_cycle_at} />. Start it with <code>rhca run</code>.
        </div>
      )}
    </>
  );
}
