/**
 * What became of each proposal, and the segments it belongs to.
 *
 * A proposal's *fate* joins three sources the page already has: the risk
 * verdict, your decision, and any execution the agent recorded. Pure
 * functions, so the server and every chart derive identical numbers.
 */

import type { Decision, Proposal } from "./types";

export type Fate = "placed" | "accepted" | "awaiting" | "declined" | "blocked" | "held";

/** Glyph + label ride with every fate color, so state never rests on hue alone. */
export const FATES: Record<Fate, { label: string; short: string; glyph: string; help: string }> = {
  placed: {
    label: "Placed",
    short: "Placed",
    glyph: "●",
    help: "An order went to Robinhood and was recorded with rhca record-execution.",
  },
  accepted: {
    label: "Accepted, not yet placed",
    short: "Accepted",
    glyph: "◐",
    help: "You accepted it; the agent has not recorded an order for it yet.",
  },
  awaiting: {
    label: "Needs your call",
    short: "Your call",
    glyph: "▲",
    help: "Cleared every risk rule and waits on an accept or decline.",
  },
  declined: { label: "Declined", short: "Declined", glyph: "○", help: "You declined it." },
  blocked: {
    label: "Blocked by risk",
    short: "Blocked",
    glyph: "■",
    help: "A blocking risk rule failed, so it was never offered.",
  },
  held: {
    label: "Held back",
    short: "Held",
    glyph: "◇",
    help: "Cleared risk, but the retired System 1 pipeline never offered it.",
  },
};

export const FATE_ORDER: Fate[] = ["placed", "accepted", "awaiting", "declined", "held", "blocked"];

export function fateOf(p: Proposal, d: Decision | undefined): Fate {
  if ((p.execution?.tranches ?? 0) > 0) return "placed";
  if (d?.decision === "accept") return "accepted";
  if (d?.decision === "decline") return "declined";
  if (p.risk_passed === false || p.status === "rejected_by_risk") return "blocked";
  return p.actionable ? "awaiting" : "held";
}

export type SegmentKey = "long" | "short" | "ladder" | "system1";

/** `slot` is the categorical palette slot: identity, fixed per segment, never by rank. */
export const SEGMENTS: Record<SegmentKey, { label: string; note: string; slot: number }> = {
  long: { label: "Long-term sleeve", note: "the split · bought low, held", slot: 1 },
  short: { label: "Short-term sleeve", note: "the split · breakout to its stop", slot: 2 },
  ladder: { label: "Trend ladder", note: "retired 2026-09-28", slot: 3 },
  system1: { label: "System 1", note: "retired 2026-09-27", slot: 7 },
};

export const SEGMENT_ORDER: SegmentKey[] = ["long", "short", "ladder", "system1"];

export function segmentOf(p: Proposal): SegmentKey {
  if (p.strategy === "split") return p.sleeve === "long-term" ? "long" : "short";
  if (p.strategy === "ladder") return "ladder";
  return "system1";
}

export interface Tally {
  total: number;
  fates: Record<Fate, number>;
  wins: number;
  losses: number;
}

function emptyTally(): Tally {
  return {
    total: 0,
    fates: { placed: 0, accepted: 0, awaiting: 0, declined: 0, blocked: 0, held: 0 },
    wins: 0,
    losses: 0,
  };
}

export type Fated = { p: Proposal; fate: Fate; segment: SegmentKey };

export function withFates(proposals: Proposal[], decisions: Decision[]): Fated[] {
  const byId = new Map(decisions.map((d) => [d.proposal_id, d]));
  return proposals.map((p) => ({ p, fate: fateOf(p, byId.get(p.proposal_id)), segment: segmentOf(p) }));
}

export function tally(rows: Fated[], keyOf: (r: Fated) => string): Map<string, Tally> {
  const out = new Map<string, Tally>();
  for (const r of rows) {
    const key = keyOf(r);
    const t = out.get(key) ?? emptyTally();
    t.total++;
    t.fates[r.fate]++;
    if (r.p.outcome?.verdict === "win") t.wins++;
    if (r.p.outcome?.verdict === "loss") t.losses++;
    out.set(key, t);
  }
  return out;
}

export type Metric = "cleared" | "accepted" | "placed" | "won";

/**
 * Each "success rate" as a share of the proposals it could apply to. Null
 * means no evidence yet, which is never drawn as 0%.
 */
export const METRICS: Record<Metric, { label: string; of: string; rate: (t: Tally) => [number, number] }> = {
  cleared: {
    label: "Cleared risk",
    of: "of proposals cleared every risk rule",
    rate: (t) => [t.total - t.fates.blocked, t.total],
  },
  accepted: {
    label: "Accepted",
    of: "of your decisions were accepts",
    rate: (t) => [t.fates.placed + t.fates.accepted, t.fates.placed + t.fates.accepted + t.fates.declined],
  },
  placed: {
    label: "Placed",
    of: "of offered proposals became orders",
    rate: (t) => [t.fates.placed, t.total - t.fates.blocked - t.fates.held],
  },
  won: {
    label: "Won",
    of: "of scored outcomes were wins",
    rate: (t) => [t.wins, t.wins + t.losses],
  },
};

export function rateOf(metric: Metric, t: Tally): { hits: number; of: number; rate: number | null } {
  const [hits, of] = METRICS[metric].rate(t);
  return { hits, of, rate: of > 0 ? hits / of : null };
}

/** Sankey nodes, by column: all → risk verdict → your call → placement. */
export type FlowNode = "all" | "cleared" | "blocked" | "accepted" | "declined" | "awaiting" | "held" | "placed";

export const FLOW_NODES: Record<FlowNode, { label: string; short: string; col: number; tone: string }> = {
  all: { label: "Proposed", short: "Proposed", col: 0, tone: "ink" },
  cleared: { label: "Cleared risk", short: "Cleared", col: 1, tone: "ink-2" },
  blocked: { label: "Blocked by risk", short: "Blocked", col: 1, tone: "blocked" },
  accepted: { label: "Accepted", short: "Accepted", col: 2, tone: "accepted" },
  awaiting: { label: "Needs your call", short: "Your call", col: 2, tone: "awaiting" },
  declined: { label: "Declined", short: "Declined", col: 2, tone: "declined" },
  held: { label: "Held back", short: "Held", col: 2, tone: "held" },
  placed: { label: "Placed", short: "Placed", col: 3, tone: "placed" },
};

export const FLOW_ORDER: FlowNode[] = ["all", "cleared", "blocked", "accepted", "awaiting", "declined", "held", "placed"];

/** One proposal's route through the columns. */
export function routeOf(fate: Fate): FlowNode[] {
  if (fate === "blocked") return ["all", "blocked"];
  const call: FlowNode = fate === "placed" ? "accepted" : fate;
  return fate === "placed" ? ["all", "cleared", call, "placed"] : ["all", "cleared", call];
}

export function flowLinks(rows: Fated[]): { nodes: Map<FlowNode, number>; links: Map<string, number> } {
  const nodes = new Map<FlowNode, number>();
  const links = new Map<string, number>();
  for (const { fate } of rows) {
    const route = routeOf(fate);
    route.forEach((n, i) => {
      nodes.set(n, (nodes.get(n) ?? 0) + 1);
      if (i > 0) {
        const key = `${route[i - 1]}>${n}`;
        links.set(key, (links.get(key) ?? 0) + 1);
      }
    });
  }
  return { nodes, links };
}
