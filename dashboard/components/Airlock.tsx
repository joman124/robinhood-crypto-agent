"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { decide, undecide } from "@/app/actions";
import { RelTime } from "@/components/Live";
import { Badge, CheckRow, Icon } from "@/components/ui";
import type { Decision, DecisionKind, Proposal } from "@/lib/types";

export type AirlockCheck = { label: string; detail: string; ok: boolean };

/**
 * The final confirmation. Accept needs the proposal id typed back, so a
 * decision is always about one named proposal, never "the one on screen".
 * It records a decision; it never places an order, and says so.
 */
export function Airlock({ proposal, decision, canDecide, blocker, syncedAt, checks }: {
  proposal: Proposal;
  decision: Decision | undefined;
  canDecide: boolean;
  /** Why Accept is unavailable, or null. Decline is always available. */
  blocker: string | null;
  /** When the agent last synced: a decision older than this has been picked up. */
  syncedAt: string | null;
  checks: AirlockCheck[];
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [typed, setTyped] = useState("");
  const id = proposal.proposal_id;
  const confirmed = typed.trim() === id;

  const run = (fn: () => Promise<{ ok: boolean; error?: string }>) => {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) setError(result.error ?? "something went wrong");
      else {
        setNote("");
        setTyped("");
        // Pin the URL to this proposal, so it stays on screen once it leaves "Your call".
        router.replace(`/proposals?id=${id}`, { scroll: false });
      }
    });
  };
  const choose = (kind: DecisionKind) => run(() => decide(id, kind, note));

  return (
    <section className="card">
      <div className="card-head">
        <h2>Decide</h2>
      </div>

      <ul className="check-grid two">
        {checks.map((c) => (
          <CheckRow
            key={c.label}
            mark={<Icon name={c.ok ? "check" : "x"} size={12} />}
            label={c.label}
            detail={c.detail}
            state={c.ok ? "pass" : "block"}
          />
        ))}
      </ul>

      {decision ? (
        <div className="decided">
          <Badge tone={decision.decision === "accept" ? "mint" : "neutral"} icon={decision.decision === "accept" ? "circle-check" : "x"}>
            {decision.decision === "accept" ? "Accepted" : "Declined"}
          </Badge>
          <p className="muted small">
            <RelTime iso={decision.decided_at} /> ·{" "}
            {syncedAt !== null && Date.parse(syncedAt) > Date.parse(decision.decided_at) ? "picked up" : "awaiting sync"}
          </p>
          {decision.note && <blockquote className="decision-note">{decision.note}</blockquote>}
          {canDecide && (
            <button className="btn small" disabled={pending} onClick={() => run(() => undecide(id))}>
              Undo decision
            </button>
          )}
        </div>
      ) : (
        <div className="confirm">
          {canDecide && (
            <>
              <label className="field">
                <span>Type the ID to accept</span>
                <span className={`confirm-input ${confirmed ? "ok" : ""}`}>
                  <input
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                    placeholder={id}
                    spellCheck={false}
                    autoComplete="off"
                    autoCapitalize="none"
                    autoCorrect="off"
                    inputMode="text"
                    aria-describedby="confirm-hint"
                  />
                  {confirmed && <Icon name="circle-check" size={16} />}
                </span>
              </label>
              <label className="field">
                <span>Note</span>
                <textarea value={note} maxLength={500} rows={2} onChange={(e) => setNote(e.target.value)} />
              </label>
            </>
          )}
          <div className="confirm-actions">
            <button className="btn danger" disabled={!canDecide || pending} onClick={() => choose("decline")} title={canDecide ? "Record a decline" : "Sign in to decide"}>
              <Icon name="x" /> Decline
            </button>
            <button
              className="btn primary grow"
              disabled={!canDecide || pending || blocker !== null || !confirmed}
              title={blocker ?? (!canDecide ? "Sign in to decide" : confirmed ? "Record an accept" : "Type the proposal ID first")}
              onClick={() => choose("accept")}
            >
              <Icon name="lock-keyhole-open" /> {pending ? "Recording…" : "Accept"}
            </button>
          </div>
          {blocker && <p className="small tone-text amber" id="confirm-hint">{blocker}</p>}
        </div>
      )}
      {error && <p className="small tone-text red" role="alert">{error}</p>}
    </section>
  );
}
