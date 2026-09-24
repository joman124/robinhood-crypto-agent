"use client";

import { useState, useTransition } from "react";

import { decide, undecide } from "@/app/actions";
import { RelTime } from "@/components/Live";
import type { Decision, DecisionKind, Proposal } from "@/lib/types";

type Props = {
  proposal: Proposal;
  decision: Decision | undefined;
  canDecide: boolean;
  /** Why Accept is unavailable, or null. Decline is always available. */
  blocker: string | null;
  /** When the agent last synced: a decision older than this has been picked up. */
  syncedAt: string | null;
  /** The drawer offers a note; the table row does not. */
  withNote?: boolean;
};

export function DecisionControls({ proposal, decision, canDecide, blocker, syncedAt, withNote }: Props) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const run = (fn: () => Promise<{ ok: boolean; error?: string }>) => {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) setError(result.error ?? "something went wrong");
      else setNote("");
    });
  };
  const choose = (kind: DecisionKind) => run(() => decide(proposal.proposal_id, kind, note));

  if (decision) {
    const pickedUp = syncedAt !== null && Date.parse(syncedAt) > Date.parse(decision.decided_at);
    return (
      <div className="decision">
        <span className={`decision-chip ${decision.decision}`}>
          <span aria-hidden="true">{decision.decision === "accept" ? "✓" : "✗"}</span>
          {decision.decision === "accept" ? "Accepted" : "Declined"}
        </span>
        <div className="muted small">
          <RelTime iso={decision.decided_at} /> ·{" "}
          {pickedUp ? "picked up by the agent" : "waiting for the next sync"}
        </div>
        {withNote && decision.note && <blockquote className="decision-note">{decision.note}</blockquote>}
        {canDecide && (
          <button className="link" disabled={pending} onClick={() => run(() => undecide(proposal.proposal_id))}>
            undo
          </button>
        )}
        {error && <div className="error">{error}</div>}
      </div>
    );
  }

  if (!proposal.actionable) {
    return (
      <span className="muted small" title={blocker ?? undefined}>
        {proposal.risk_passed === false ? "blocked" : "record only"}
      </span>
    );
  }

  return (
    <div className="decision">
      {withNote && canDecide && (
        <label className="note-field">
          <span className="small muted">Note (optional, kept with the decision)</span>
          <textarea value={note} maxLength={500} rows={2} onChange={(e) => setNote(e.target.value)} />
        </label>
      )}
      <div className="actions">
        <button
          className="accept"
          disabled={!canDecide || pending || blocker !== null}
          title={blocker ?? (canDecide ? "Record an accept" : "Sign in to decide")}
          onClick={(e) => {
            e.stopPropagation();
            choose("accept");
          }}
        >
          Accept
        </button>
        <button
          className="decline"
          disabled={!canDecide || pending}
          title={canDecide ? "Record a decline" : "Sign in to decide"}
          onClick={(e) => {
            e.stopPropagation();
            choose("decline");
          }}
        >
          Decline
        </button>
      </div>
      {blocker && <div className="small warn-text">{withNote ? blocker : "Accept unavailable: see details"}</div>}
      {error && <div className="error">{error}</div>}
    </div>
  );
}
