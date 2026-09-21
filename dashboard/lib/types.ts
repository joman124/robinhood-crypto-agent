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
  proposed_at: string;
  resolved_at: string | null;
  regime: string;
  score: number;
  confidence: number;
  reason: string;
}

export interface Proposal {
  proposal_id: string;
  symbol: string;
  side: Side;
  quantity: string | null;
  reference_price: string | null;
  notional: string | null;
  status: string | null;
  risk_passed: boolean | null;
  risk_failures: string[] | null;
  score: number | null;
  confidence: number | null;
  regime: string | null;
  proposed_at: string | null;
  /** False when the risk engine blocked it — such a proposal has no Accept. */
  actionable: boolean;
  outcome: Outcome | null;
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
    by_regime: Record<string, Stats>;
    by_symbol: Record<string, Stats>;
    by_side: Record<string, Stats>;
  };
  today: {
    executions: number;
    executed_notional: string;
    realized_pnl: string;
    proposals: number;
  };
}

export interface Decision {
  proposal_id: string;
  decision: DecisionKind;
  decided_at: string;
  source: string;
  actor: string;
  note: string;
}
