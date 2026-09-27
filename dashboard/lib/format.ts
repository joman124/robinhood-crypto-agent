/**
 * Formatting and the labels the page uses for the agent's vocabulary.
 * Pure functions only, so server and client components render identical text.
 */

import type { Payload, Proposal, Verdict } from "./types";

export const DASH = "—";

export function pct(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? DASH : `${(value * 100).toFixed(digits)}%`;
}

export function money(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  const sign = n < 0 ? "−" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Prices span $0.0001 to $100k, so precision follows magnitude. */
export function price(value: string | null | undefined): string {
  if (!value) return DASH;
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  const digits = n >= 1000 ? 2 : n >= 1 ? 4 : 6;
  return `$${n.toLocaleString("en-US", { maximumFractionDigits: digits })}`;
}

export function signedPct(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)}%`;
}

export function score(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}`;
}

export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return DASH;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return DASH;
  const s = Math.round((now - t) / 1000);
  if (s < -60) return "in the future";
  if (s < 45) return "just now";
  if (s < 90) return "1m ago";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 36 * 3600) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function secondsSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : (now - t) / 1000;
}

export const STATUS: Record<string, { label: string; help: string }> = {
  proposed: {
    label: "Proposed",
    help: "Passed every risk rule.",
  },
  // The rest are System 1's, retired 2026-09-27. Its records still carry them.
  declined_by_system2: {
    label: "Passed by System 2",
    help: "Escalated, and Claude Sonnet 5 passed on it (or failed to answer, which counts as a pass).",
  },
  not_escalated: {
    label: "Held back",
    help: "Passed risk but not the escalation trigger, so System 2 never saw it.",
  },
  rejected_by_risk: {
    label: "Blocked by risk",
    help: "At least one blocking risk rule failed. Logged and scored, never offered.",
  },
};

export function statusLabel(status: string | null | undefined): string {
  return (status && STATUS[status]?.label) || status || "Proposed";
}

/** What each of the 16 rules checks, for rules whose own message is withheld. */
export const RULES: Record<string, string> = {
  execution_mode: "Mode is propose-only: a human approves each proposal by id",
  kill_switch: "Kill switch is released",
  watchlist: "Pair is on the watchlist allowlist",
  pair_tradable: "Pair is tradable with no global halt",
  order_type_supported: "Pair accepts this order type",
  quote_freshness: "Reference quote is fresh enough",
  spread: "Bid/ask spread is under the cap",
  signal_confidence: "Composite confidence is over the floor",
  signal_strength: "Composite |score| is over the floor",
  sizing: "Sizing produced a tradable quantity",
  per_trade_notional: "Worst case after the collar is under the per-trade cap",
  daily_notional: "Today's traded dollars stay under the daily cap",
  daily_loss: "Today's realized loss is under the cap",
  open_positions: "Open positions stay under the cap",
  concentration: "The pair's share of the portfolio stays under the cap",
  sell_coverage: "Enough is held to cover the sell",
};

export function ruleLabel(rule: string): string {
  return rule.replace(/_/g, " ");
}

export function isLadder(p: Proposal): boolean {
  return p.strategy === "ladder";
}

/** "Dip, step 1", "Take profit, step 2", "Trend exit". Steps arrive 0-based. */
export function ladderLabel(p: Proposal): string {
  const rule = ruleLabel(p.rule || "ladder");
  const name = rule[0].toUpperCase() + rule.slice(1);
  return p.step == null ? name : `${name}, step ${p.step + 1}`;
}

/** The outcome, or null for a ladder proposal, which is measured on P&L and never scored. */
export function verdictOf(p: Proposal): Verdict | null {
  return p.outcome?.verdict ?? (isLadder(p) ? null : "pending");
}

/**
 * Why Accept is unavailable, or null when it is available. Mirrors what the
 * agent's gate would refuse, so the page never offers what it would reject.
 * The gate re-checks all of it against a fresh quote regardless.
 */
export function acceptBlocker(p: Proposal, payload: Payload | null): string | null {
  if (!p.actionable) {
    return p.risk_passed === false
      ? "The risk engine blocked this. Overriding it is a typed act in the terminal."
      : `${statusLabel(p.status)}: a record, not a proposal. Run rhca analyze for a fresh one.`;
  }
  if (payload?.kill_switch?.engaged) {
    return "The kill switch is engaged, so the gate refuses every execution.";
  }
  const tolerance = Number(payload?.limits?.price_drift_tolerance_pct);
  const drift = Number(p.drift_pct);
  if (p.drift_pct && Number.isFinite(drift) && Number.isFinite(tolerance) && drift > tolerance) {
    return `The price has moved ${drift.toFixed(2)}% since this was proposed (tolerance ${tolerance}%). The gate would refuse it; wait for a fresh proposal.`;
  }
  return null;
}

/** RFC 4180 CSV of the proposal log, for the questions the page does not answer. */
export function toCsv(rows: Proposal[]): string {
  const cols: [string, (p: Proposal) => unknown][] = [
    ["proposal_id", (p) => p.proposal_id],
    ["proposed_at", (p) => p.proposed_at],
    ["symbol", (p) => p.symbol],
    ["side", (p) => p.side],
    ["status", (p) => p.status],
    ["strategy", (p) => p.strategy],
    ["rule", (p) => p.rule],
    ["step", (p) => p.step],
    ["notional", (p) => p.notional],
    ["reference_price", (p) => p.reference_price],
    ["score", (p) => p.score],
    ["confidence", (p) => p.confidence],
    ["regime", (p) => p.regime],
    ["risk_passed", (p) => p.risk_passed],
    ["risk_failures", (p) => (p.risk_failures ?? []).join(" ")],
    ["system2_decision", (p) => p.system2_decision],
    ["verdict", (p) => verdictOf(p)],
    ["signed_move_pct", (p) => p.outcome?.signed_move_pct],
    ["resolved_at", (p) => p.outcome?.resolved_at],
  ];
  const cell = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.map(([h]) => h).join(","), ...rows.map((r) => cols.map(([, f]) => cell(f(r))).join(","))].join("\n");
}
