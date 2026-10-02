"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { RelTime, useNow } from "@/components/Live";
import { Badge, Icon, type IconName, type Tone } from "@/components/ui";
import { type Loop, health } from "@/lib/health";
import type { Payload } from "@/lib/types";

const NAV: { href: string; label: string; icon: IconName }[] = [
  { href: "/", label: "Command center", icon: "layout-dashboard" },
  { href: "/proposals", label: "Proposals", icon: "file-check" },
  { href: "/risk", label: "Risk engine", icon: "shield-check" },
  { href: "/strategy", label: "Strategy", icon: "split" },
  { href: "/audit", label: "Audit trail", icon: "scroll-text" },
];

/** Old bookmarks pointed at `/#p=<id>`; the proposal now lives on its own route. */
const LEGACY_HASH = /^#p=([0-9a-f]{6,64})$/;

export function Nav({ awaiting }: { awaiting: number }) {
  const path = usePathname();
  const router = useRouter();

  useEffect(() => {
    const id = LEGACY_HASH.exec(window.location.hash)?.[1];
    if (id) router.replace(`/proposals?id=${id}`);
  }, [router]);

  return (
    <nav className="nav" aria-label="Console">
      {NAV.map((item) => {
        const active = item.href === "/" ? path === "/" : path.startsWith(item.href);
        return (
          <Link key={item.href} href={item.href} className={active ? "nav-item on" : "nav-item"} aria-current={active ? "page" : undefined}>
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
            {item.href === "/proposals" && awaiting > 0 && (
              <span className="nav-count" aria-label={`${awaiting} waiting on you`}>{awaiting}</span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

const LOOP_FACE: Record<Loop, { label: string; tone: Tone }> = {
  running: { label: "Running", tone: "mint" },
  stopped: { label: "Not running", tone: "amber" },
  unknown: { label: "No word", tone: "amber" },
  never: { label: "Never run", tone: "neutral" },
};

export function AgentState({ payload }: { payload: Payload | null }) {
  const { now } = useNow();
  const loop: Loop = payload ? health(payload, now).loop : "never";
  const face = LOOP_FACE[loop];
  return (
    <div className="agent-state">
      <div className="agent-state-head">
        <span className="eyebrow">Shadow loop</span>
        <span className={`dot ${face.tone} ${loop === "running" ? "pulse" : ""}`} aria-hidden="true" />
      </div>
      <strong>{face.label}</strong>
      {payload?.pipeline?.last_cycle_at && (
        <span className="muted small"><RelTime iso={payload.pipeline.last_cycle_at} /></span>
      )}
    </div>
  );
}

export function TopBar({ payload, readOnly, memory }: { payload: Payload | null; readOnly: boolean; memory: boolean }) {
  const { now } = useNow();
  const syncStale = payload ? health(payload, now).syncStale : true;
  const kill = payload?.kill_switch;
  const quotes = payload?.pipeline?.services?.robinhood;
  const mode = payload?.execution_mode ?? "propose_only";

  return (
    <div className="topbar">
      <div className="sources" aria-label="Data sources">
        <span><i className={`dot ${quotes ? "mint" : "neutral"}`} aria-hidden="true" />Robinhood quotes</span>
        <span>
          <i className={`dot ${syncStale ? "amber" : "mint"}`} aria-hidden="true" />
          Last sync {payload ? <RelTime iso={payload.generated_at} /> : "never"}
        </span>
      </div>
      <div className="topbar-badges">
        <Badge tone="red" icon="triangle-alert" title="Real money. This console records decisions; it never places an order.">Real money</Badge>
        <Badge tone="sky" icon="eye">{mode.replace(/_/g, "-")}</Badge>
        {readOnly && <Badge tone="amber" icon="lock-keyhole" title="DASHBOARD_PASSWORD is not set">Read-only</Badge>}
        {memory && <Badge tone="amber" icon="info" title="No KV store: decisions are lost on a cold start">Memory storage</Badge>}
        {kill && (
          <Badge tone={kill.engaged ? "red" : "mint"} icon="power">
            {kill.engaged ? "Kill switch engaged" : "Kill switch armed"}
          </Badge>
        )}
      </div>
    </div>
  );
}

/** The one banner: a tripped kill switch halts trading. Staleness shows in the top bar. */
export function StatusAlerts({ payload }: { payload: Payload | null }) {
  if (!payload) return null;

  const kill = payload.kill_switch;

  return (
    <>
      {kill?.engaged && (
        <div className="alert critical" role="alert">
          <Icon name="octagon-x" />
          <p>
            <strong>Kill switch engaged</strong>
            {kill.reason ? ` · ${kill.reason.replace(/_/g, " ")}` : ""}
            {kill.engaged_at ? <> · <RelTime iso={kill.engaged_at} /></> : null}
          </p>
        </div>
      )}
    </>
  );
}

/** The split decides on Coinbase daily closes, so the next decision is the next 00:00 UTC. */
export function SignalClock() {
  // Its own one-second clock: the page clock ticks every 15s, too coarse for seconds.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (now === null) {
    return (
      <div className="countdown"><span className="countdown-value">--:--:--</span><span className="eyebrow">UTC</span></div>
    );
  }
  const next = new Date(now);
  next.setUTCHours(24, 0, 0, 0);
  const left = Math.max(0, Math.floor((next.getTime() - now) / 1000));
  const hms = [left / 3600, (left % 3600) / 60, left % 60].map((n) => String(Math.floor(n)).padStart(2, "0"));
  return (
    <div className="countdown" aria-live="off">
      <span className="countdown-value">{hms.join(":")}</span>
      <span className="eyebrow">UTC</span>
    </div>
  );
}
