"use client";

import { useState, useTransition } from "react";

import { decide, undecide } from "@/app/actions";
import { VerdictChip } from "@/components/Verdict";
import type { Decision, Proposal } from "@/lib/types";

function when(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function money(value: string | null): string {
  if (!value) return "—";
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : value;
}

function DecisionCell({
  proposal,
  decision,
  canDecide,
}: {
  proposal: Proposal;
  decision: Decision | undefined;
  canDecide: boolean;
}) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const run = (fn: () => Promise<{ ok: boolean; error?: string }>) => {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) setError(result.error ?? "something went wrong");
    });
  };

  if (decision) {
    return (
      <div>
        <span className={`decision-chip ${decision.decision}`}>
          <span aria-hidden="true">{decision.decision === "accept" ? "✓" : "✗"}</span>
          {decision.decision === "accept" ? "Accepted" : "Declined"}
        </span>
        {canDecide && (
          <div>
            <button
              className="link"
              disabled={pending}
              onClick={() => run(() => undecide(proposal.proposal_id))}
            >
              undo
            </button>
          </div>
        )}
        {error && <div className="error">{error}</div>}
      </div>
    );
  }

  if (!proposal.actionable) {
    // A risk-blocked proposal is shown for the record but is not offerable.
    // Overriding the risk engine stays a deliberate act in the terminal.
    return <span className="chip blocked" title="Blocked by the risk engine">—</span>;
  }

  return (
    <div>
      <div className="actions">
        <button
          className="accept"
          disabled={!canDecide || pending}
          title={canDecide ? "Record an accept" : "Sign in to decide"}
          onClick={() => run(() => decide(proposal.proposal_id, "accept"))}
        >
          Accept
        </button>
        <button
          className="decline"
          disabled={!canDecide || pending}
          onClick={() => run(() => decide(proposal.proposal_id, "decline"))}
        >
          Decline
        </button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

type Filter = "awaiting" | "decided" | "all";

export function ProposalTable({
  proposals,
  decisions,
  canDecide,
}: {
  proposals: Proposal[];
  decisions: Decision[];
  canDecide: boolean;
}) {
  const byId = new Map(decisions.map((d) => [d.proposal_id, d]));

  // "Awaiting" is the working set: actionable, and not yet decided. Most rows
  // in a mature log are history, so defaulting to the whole list buries the
  // handful that actually need an answer.
  const awaiting = proposals.filter((p) => p.actionable && !byId.has(p.proposal_id));
  const decided = proposals.filter((p) => byId.has(p.proposal_id));

  const [filter, setFilter] = useState<Filter>(awaiting.length > 0 ? "awaiting" : "all");

  const shown =
    filter === "awaiting" ? awaiting : filter === "decided" ? decided : proposals;

  if (proposals.length === 0) {
    return (
      <div className="table-wrap">
        <div className="empty">
          <p>No proposals yet.</p>
          <p>
            Run <code>rhca dashboard-sync</code> where the agent lives to push them here.
          </p>
        </div>
      </div>
    );
  }

  const tabs: { key: Filter; label: string; count: number }[] = [
    { key: "awaiting", label: "Needs your call", count: awaiting.length },
    { key: "decided", label: "Decided", count: decided.length },
    { key: "all", label: "All", count: proposals.length },
  ];

  return (
    <>
      <div className="filters" role="tablist" aria-label="Filter proposals">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={filter === tab.key}
            className={filter === tab.key ? "filter on" : "filter"}
            onClick={() => setFilter(tab.key)}
          >
            {tab.label} <span className="count">{tab.count}</span>
          </button>
        ))}
      </div>
      <div className="table-wrap">
      {shown.length === 0 ? (
        <div className="empty">
          <p>
            {filter === "awaiting"
              ? "Nothing is waiting on you — every actionable proposal has been decided."
              : "Nothing here yet."}
          </p>
        </div>
      ) : (
      <table>
        <thead>
          <tr>
            <th>Proposed</th>
            <th>Pair</th>
            <th>Side</th>
            <th>Notional</th>
            <th>Signal</th>
            <th>Regime</th>
            <th>Risk</th>
            <th>Outcome</th>
            <th>Your call</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((p) => (
            <tr key={p.proposal_id}>
              <td>
                {when(p.proposed_at)}
                <div className="pid">{p.proposal_id}</div>
              </td>
              <td className="sym">{p.symbol}</td>
              <td>
                <span className={p.side === "buy" ? "side-buy" : "side-sell"}>
                  {p.side.toUpperCase()}
                </span>
              </td>
              <td className="num">{money(p.notional)}</td>
              <td className="num">
                {p.score !== null ? p.score.toFixed(2) : "—"}
                <span style={{ color: "var(--text-muted)" }}>
                  {p.confidence !== null ? ` · ${(p.confidence * 100).toFixed(0)}%` : ""}
                </span>
              </td>
              <td>{p.regime ?? "—"}</td>
              <td>
                {p.risk_passed ? (
                  <span className="chip ok">
                    <span className="glyph" aria-hidden="true">
                      ✓
                    </span>
                    Passed
                  </span>
                ) : (
                  <span
                    className="chip blocked"
                    title={(p.risk_failures ?? []).join(", ") || "blocked"}
                  >
                    <span className="glyph" aria-hidden="true">
                      ✗
                    </span>
                    {(p.risk_failures ?? []).join(", ") || "Blocked"}
                  </span>
                )}
              </td>
              <td>
                {p.outcome ? (
                  <>
                    <VerdictChip verdict={p.outcome.verdict} title={p.outcome.reason} />
                    {p.outcome.signed_move_pct !== null && (
                      <div className="pid">{p.outcome.signed_move_pct}%</div>
                    )}
                  </>
                ) : (
                  <VerdictChip verdict="pending" />
                )}
              </td>
              <td>
                <DecisionCell
                  proposal={p}
                  decision={byId.get(p.proposal_id)}
                  canDecide={canDecide}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}
      </div>
    </>
  );
}
