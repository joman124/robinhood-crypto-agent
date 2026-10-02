/**
 * The audit trail this console can honestly show: every timestamped thing in
 * the synced payload and the decision log, as one newest-first sequence.
 *
 * It is a *view*, not the agent's append-only log (that stays on the agent's
 * machine, `rhca audit`). Pure, so it can be self-checked.
 */

import { type Fate, fateOf } from "./flow";
import { coinOf, isLadder, ladderLabel } from "./format";
import type { Decision, Payload } from "./types";

export type EventType = "proposal" | "risk" | "decision" | "control" | "sync";

export interface AuditEvent {
  key: string;
  at: string;
  type: EventType;
  actor: string;
  event: string;
  ref: string;
  result: string;
  tone: "mint" | "amber" | "red" | "sky" | "violet" | "neutral";
  /** What was synced for it, shown verbatim in the detail panel. */
  data: unknown;
}

const RESULT: Record<Fate, [string, AuditEvent["tone"]]> = {
  placed: ["Placed", "mint"],
  accepted: ["Accepted", "mint"],
  awaiting: ["Awaiting call", "amber"],
  declined: ["Declined", "neutral"],
  held: ["Held back", "neutral"],
  blocked: ["Blocked", "red"],
};

export function eventsOf(payload: Payload | null, decisions: Decision[]): AuditEvent[] {
  const out: AuditEvent[] = [];
  const byId = new Map(decisions.map((d) => [d.proposal_id, d]));

  for (const p of payload?.proposals ?? []) {
    if (!p.proposed_at) continue;
    const blocked = p.risk_passed === false;
    const [result, tone] = RESULT[fateOf(p, byId.get(p.proposal_id))];
    out.push({
      key: `p-${p.proposal_id}`,
      at: p.proposed_at,
      type: blocked ? "risk" : "proposal",
      actor: `agent:${p.strategy ?? "system1"}`,
      event: `${coinOf(p.symbol)} ${p.side} · ${isLadder(p) ? ladderLabel(p) : "System 1 candidate"}`,
      ref: p.proposal_id,
      result: blocked ? `Blocked · ${(p.risk_failures ?? []).join(", ") || "risk"}` : result,
      tone: blocked ? "red" : tone,
      data: p,
    });
  }

  for (const d of decisions) {
    const picked = payload ? Date.parse(payload.generated_at) > Date.parse(d.decided_at) : false;
    out.push({
      key: `d-${d.proposal_id}-${d.decided_at}`,
      at: d.decided_at,
      type: "decision",
      actor: `${d.source}:${d.actor}`,
      event: d.decision === "accept" ? "Proposal ID accepted" : "Proposal declined",
      ref: d.proposal_id,
      result: picked ? "Picked up by agent" : "Awaiting sync",
      tone: d.decision === "accept" ? "mint" : "neutral",
      data: d,
    });
  }

  if (payload) {
    out.push({
      key: `s-${payload.generated_at}`,
      at: payload.generated_at,
      type: "sync",
      actor: "agent:dashboard-sync",
      event: "Payload pushed to the console",
      ref: `v${payload.version}`,
      result: `${payload.proposals.length} proposals`,
      tone: "sky",
      data: { version: payload.version, generated_at: payload.generated_at, execution_mode: payload.execution_mode, watchlist: payload.watchlist, limits: payload.limits },
    });
    const kill = payload.kill_switch;
    if (kill?.engaged && kill.engaged_at) {
      out.push({ key: "k", at: kill.engaged_at, type: "control", actor: "system", event: "Kill switch engaged", ref: "GLOBAL", result: kill.reason?.replace(/_/g, " ") ?? "Engaged", tone: "red", data: kill });
    }
    const beat = payload.pipeline;
    if (beat?.started_at) {
      out.push({ key: "l", at: beat.started_at, type: "control", actor: "system", event: "Shadow loop started", ref: beat.mode ?? "shadow", result: `${beat.cycles ?? 0} cycles since`, tone: "violet", data: { mode: beat.mode, started_at: beat.started_at, cycles: beat.cycles, services: beat.services } });
    }
    if (beat?.last_error?.at) {
      out.push({ key: "e", at: beat.last_error.at, type: "control", actor: "system", event: `Loop error in ${beat.last_error.task ?? "a task"}`, ref: "rhca status", result: "Text kept on agent", tone: "amber", data: beat.last_error });
    }
  }

  return out.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}
