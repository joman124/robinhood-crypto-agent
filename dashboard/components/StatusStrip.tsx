"use client";

import { RelTime, useNow } from "@/components/Live";
import { secondsSince } from "@/lib/format";
import type { Payload } from "@/lib/types";

/** A manual `rhca dashboard-sync` has no schedule to be late for, so give it an hour. */
const MANUAL_SYNC_STALE_SECONDS = 3600;

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

  const beat = payload.pipeline;
  const staleAfter = beat?.stale_after_seconds ?? 180;
  const loopAge = secondsSince(beat?.last_cycle_at, now);
  const loopRunning = loopAge !== null && loopAge <= staleAfter;

  const syncStale =
    (secondsSince(payload.generated_at, now) ?? Infinity) >
    (beat?.services?.dashboard ? staleAfter : MANUAL_SYNC_STALE_SECONDS);

  const kill = payload.kill_switch;

  return (
    <div className="strip" aria-label="Agent status">
      {beat === undefined ? null : beat === null ? (
        <Pill tone="neutral" label="Shadow loop">
          never run
        </Pill>
      ) : (
        <Pill tone={loopRunning ? "good" : "warn"} label="Shadow loop">
          {loopRunning ? "running" : "not running"} · <RelTime iso={beat.last_cycle_at} />
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

  const beat = payload.pipeline;
  const staleAfter = beat?.stale_after_seconds ?? 180;
  const syncAge = secondsSince(payload.generated_at, now) ?? 0;
  const syncStale = syncAge > (beat?.services?.dashboard ? staleAfter : MANUAL_SYNC_STALE_SECONDS);
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
          {beat?.services?.dashboard ? (
            <>
              The shadow loop syncs every cycle, so it has probably stopped: check{" "}
              <code>rhca status</code>.
            </>
          ) : (
            <>
              Run <code>rhca dashboard-sync</code>, or set <code>RHCA_DASHBOARD_URL</code> and{" "}
              <code>RHCA_DASHBOARD_TOKEN</code> in the agent&apos;s <code>.env</code> so{" "}
              <code>rhca run</code> syncs every cycle.
            </>
          )}
        </div>
      )}
    </>
  );
}
