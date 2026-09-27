/**
 * Shapes pushed by `rhca dashboard-sync`.
 *
 * Deliberately narrow: the agent never sends account numbers, buying power,
 * portfolio value or order ids. The dashboard shows what the agent *suggested*
 * and whether those suggestions were any good — it does not need to know how
 * much money sits behind them, and data it never receives cannot leak.
 */

export type Verdict = "win" | "loss" | "flat" | "pending" | "unscorable";
export type Side = "buy" | "sell";
export type DecisionKind = "accept" | "decline";

export interface Outcome {
  proposal_id: string;
  symbol: string;
  side: Side;
  verdict: Verdict;
  reference_price: string;
  resolved_price: string | null;
  signed_move_pct: string | null;
  horizon_bars: number;
  hurdle_pct: string;
  exit_cost_pct?: string;
  proposed_at: string;
  resolved_at: string | null;
  // System 1's. Only its records are hit-rate scored, but don't rely on them.
  regime?: string;
  score?: number;
  confidence?: number;
  reason: string;
}

export type LadderRule = "dip" | "take_profit" | "trend_exit";

export type ProposalStatus =
  | "proposed"
  | "rejected_by_risk"
  | "not_escalated"
  | "declined_by_system2";

export interface Signal {
  name: string;
  score: number | null;
  confidence: number | null;
  direction: string | null;
  weight: number | null;
  rationale: string;
}

/** `message` is present only for rules whose text says nothing about the account. */
export interface RiskFinding {
  rule: string;
  passed: boolean;
  blocking: boolean;
  message?: string;
}

export interface ExecutionSummary {
  tranches: number;
  filled_quantity: string;
  state: string | null;
  overridden: boolean;
}

export interface Proposal {
  proposal_id: string;
  symbol: string;
  side: Side;
  quantity: string | null;
  reference_price: string | null;
  notional: string | null;
  status: ProposalStatus | string | null;
  risk_passed: boolean | null;
  risk_failures: string[] | null;
  proposed_at: string | null;
  /** False when the risk engine blocked it — such a proposal has no Accept. */
  actionable: boolean;
  /** Always null for a ladder proposal: the ladder is measured on P&L, not hit rate. */
  outcome: Outcome | null;
  /** "ladder" for the trend ladder's proposals; absent on the retired System 1's. */
  strategy?: string | null;
  rule?: LadderRule | string | null;
  /** 0-based ladder step; null for the trend exit. */
  step?: number | null;
  // The retired System 1's. Null or absent on ladder proposals.
  score?: number | null;
  confidence?: number | null;
  regime?: string | null;
  // Payload v2. Optional so a v1 push still renders.
  trigger_reason?: string | null;
  system2_decision?: string | null;
  system2_confidence?: number | null;
  signals?: Signal[];
  notes?: string[];
  plan?: { style: string | null; tranches: number; rationale: string } | null;
  risk_findings?: RiskFinding[];
  /** |last mark − reference| as a %, as of the sync — the gate's own measure. */
  drift_pct?: string | null;
  execution?: ExecutionSummary | null;
}

export interface Heartbeat {
  mode: string | null;
  started_at: string | null;
  last_cycle_at: string | null;
  cycles: number | null;
  counts: Record<string, number>;
  services: Record<string, boolean>;
  last_error: { task: string | null; at: string | null } | null;
  stale_after_seconds: number;
  /** How often `rhca run` pushes here. Absent from payloads before 2026-09-24. */
  sync_interval_seconds?: number;
}

export interface Stats {
  total: number;
  wins: number;
  losses: number;
  flat: number;
  pending: number;
  unscorable: number;
  resolved: number;
  decided: number;
  /** null means "no evidence yet" — never render it as 0%. */
  win_rate: number | null;
  average_move_pct: string | null;
  best_move_pct: string | null;
  worst_move_pct: string | null;
}

export interface Payload {
  version: number;
  generated_at: string;
  repo_url: string | null;
  execution_mode: string;
  watchlist: string[];
  scoring: {
    horizon_bars: number;
    hurdle_pct: string;
    bar_interval_minutes: number;
  };
  proposals: Proposal[];
  stats: {
    overall: Stats;
    by_regime?: Record<string, Stats>;
    by_symbol?: Record<string, Stats>;
    by_side?: Record<string, Stats>;
    by_status?: Record<string, Stats>;
  };
  today: {
    executions: number;
    executed_notional: string;
    realized_pnl: string;
    proposals: number;
  };
  /** The ladder's realized P&L per symbol, from recorded fills. Absent before 2026-09-27. */
  ladder_realized_pnl?: Record<string, string>;
  limits?: { price_drift_tolerance_pct: string };
  market?: Record<string, { mark: string; observed_at: string }>;
  kill_switch?: { engaged: boolean; reason: string | null; engaged_at: string | null };
  pipeline?: Heartbeat | null;
}

export interface Decision {
  proposal_id: string;
  decision: DecisionKind;
  decided_at: string;
  source: string;
  actor: string;
  note: string;
}
