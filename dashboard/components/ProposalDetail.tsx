"use client";

import { useEffect, useRef, useState } from "react";

import { DecisionControls } from "@/components/DecisionControls";
import { RelTime } from "@/components/Live";
import { StatusChip, VerdictChip } from "@/components/Verdict";
import { DASH, RULES, acceptBlocker, money, pct, price, ruleLabel, score, signedPct } from "@/lib/format";
import type { Decision, Payload, Proposal } from "@/lib/types";

function Copy({ text, label = "copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="link"
      onClick={() =>
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
    >
      {done ? "copied" : label}
    </button>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * A signal's score on a −1…+1 track, drawn from the centre. Long reads blue
 * and short red (the diverging pair, not the status colours), and the signed
 * number beside it carries the meaning without the hue.
 */
function SignalRow({ name, value, weight, confidence, rationale }: {
  name: string; value: number | null; weight: number | null; confidence: number | null; rationale: string;
}) {
  const v = Math.max(-1, Math.min(1, value ?? 0));
  return (
    <li className="signal">
      <div className="signal-head">
        <span className="signal-name">{name.replace(/_/g, " ")}</span>
        <span className="signal-track" aria-hidden="true">
          <span
            className={v >= 0 ? "signal-bar long" : "signal-bar short"}
            style={v >= 0 ? { left: "50%", width: `${v * 50}%` } : { right: "50%", width: `${-v * 50}%` }}
          />
        </span>
        <span className="signal-score">{score(value)}</span>
      </div>
      <div className="signal-meta muted small">
        weight {weight === null ? DASH : weight.toFixed(2)} · confidence {pct(confidence)}
        {rationale ? ` · ${rationale}` : ""}
      </div>
    </li>
  );
}

export function ProposalDetail({
  proposal: p,
  payload,
  decision,
  canDecide,
  onClose,
}: {
  proposal: Proposal | null;
  payload: Payload | null;
  decision: Decision | undefined;
  canDecide: boolean;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (p && !dialog.open) dialog.showModal();
    if (!p && dialog.open) dialog.close();
  }, [p]);

  const blocker = p ? acceptBlocker(p, payload) : null;
  const mark = p ? payload?.market?.[p.symbol] : undefined;
  const tolerance = payload?.limits?.price_drift_tolerance_pct;
  const findings = p?.risk_findings ?? [];
  const failed = findings.filter((f) => !f.passed);

  return (
    <dialog
      ref={ref}
      className="drawer"
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) ref.current?.close(); // backdrop click
      }}
      aria-labelledby="detail-title"
    >
      {p && (
        <div className="drawer-body">
          <header className="drawer-head">
            <div>
              <h2 id="detail-title">
                {p.symbol} <span className={`side ${p.side}`}>{p.side === "buy" ? "▲ BUY" : "▼ SELL"}</span>
              </h2>
              <div className="muted small">
                <code>{p.proposal_id}</code> <Copy text={p.proposal_id} /> · proposed{" "}
                <RelTime iso={p.proposed_at} />
              </div>
            </div>
            <button className="close" onClick={() => ref.current?.close()} aria-label="Close">
              ✕
            </button>
          </header>

          <section>
            <h3>Your call</h3>
            <DecisionControls
              proposal={p}
              decision={decision}
              canDecide={canDecide}
              blocker={blocker}
              syncedAt={payload?.generated_at ?? null}
              withNote
            />
            {!p.actionable && blocker && <p className="muted small">{blocker}</p>}
            <p className="muted small">
              Accepting records a decision; it never places an order. The agent picks it up at the
              next sync and still runs the kill switch, a fresh price-drift check, remaining-quantity
              accounting and order validation first.
            </p>
          </section>

          <section>
            <h3>Trade</h3>
            <dl className="facts">
              <Fact label="Stage"><StatusChip status={p.status} /></Fact>
              <Fact label="Notional">{money(p.notional)}</Fact>
              <Fact label="Quantity">{p.quantity ?? DASH}</Fact>
              <Fact label="Reference price">{price(p.reference_price)}</Fact>
              <Fact label="Last mark at sync">
                {mark ? price(mark.mark) : DASH}
                {p.drift_pct != null && (
                  <span className={blocker?.startsWith("The price") ? "warn-text" : "muted"}>
                    {" "}· {Number(p.drift_pct).toFixed(2)}% drift{tolerance ? ` (limit ${tolerance}%)` : ""}
                  </span>
                )}
              </Fact>
              <Fact label="Plan">
                {p.plan ? `${p.plan.style} · ${p.plan.tranches} tranche${p.plan.tranches === 1 ? "" : "s"}` : DASH}
              </Fact>
              <Fact label="Regime">{p.regime ?? DASH}</Fact>
              <Fact label="Composite">
                {score(p.score)} · {pct(p.confidence)} confidence
              </Fact>
            </dl>
            {p.plan?.rationale && <p className="muted small">{p.plan.rationale}</p>}
            {(p.notes ?? []).map((n) => (
              <p key={n} className="muted small">{n}</p>
            ))}
          </section>

          {(p.trigger_reason || p.system2_decision) && (
            <section>
              <h3>Pipeline</h3>
              <dl className="facts">
                {p.trigger_reason && <Fact label="Trigger">{p.trigger_reason}</Fact>}
                {p.system2_decision && (
                  <Fact label="System 2">
                    {p.system2_decision}
                    {p.system2_confidence != null ? ` · ${pct(p.system2_confidence)} confidence` : ""}
                  </Fact>
                )}
              </dl>
              {p.system2_decision && (
                <p className="muted small">
                  System 2&apos;s written rationale stays on the agent&apos;s machine (it may quote
                  holdings). Read it with <code>rhca audit --proposal-id {p.proposal_id}</code>.
                </p>
              )}
            </section>
          )}

          {(p.signals ?? []).length > 0 && (
            <section>
              <h3>Signals</h3>
              <p className="muted small">
                Each scores −1 (short) to +1 (long). The composite weights them by regime and by how
                sure each one is.
              </p>
              <ul className="signals">
                {p.signals!.map((s) => (
                  <SignalRow
                    key={s.name}
                    name={s.name}
                    value={s.score}
                    weight={s.weight}
                    confidence={s.confidence}
                    rationale={s.rationale}
                  />
                ))}
              </ul>
            </section>
          )}

          {findings.length > 0 && (
            <section>
              <h3>
                Risk verdict{" "}
                <span className={failed.length ? "warn-text" : "good-text"}>
                  {failed.length ? `blocked by ${failed.length}` : `all ${findings.length} passed`}
                </span>
              </h3>
              <ul className="rules">
                {findings.map((f) => (
                  <li key={f.rule} className={f.passed ? "pass" : f.blocking ? "fail" : "warn"}>
                    <span className="rule-glyph" aria-hidden="true">
                      {f.passed ? "✓" : f.blocking ? "✗" : "!"}
                    </span>
                    <span className="rule-name">{ruleLabel(f.rule)}</span>
                    <span className="rule-msg">{f.message ?? RULES[f.rule] ?? ""}</span>
                  </li>
                ))}
              </ul>
              <p className="muted small">
                Rules that would quote the account (sizing, dollar caps, positions, concentration,
                coverage) show their verdict here and their numbers only in <code>rhca analyze -v</code>.
              </p>
            </section>
          )}

          <section>
            <h3>Outcome</h3>
            {p.outcome ? (
              <dl className="facts">
                <Fact label="Verdict"><VerdictChip verdict={p.outcome.verdict} /></Fact>
                <Fact label="Move in its favour">{signedPct(p.outcome.signed_move_pct)}</Fact>
                <Fact label="Price">
                  {price(p.outcome.reference_price)} → {price(p.outcome.resolved_price)}
                </Fact>
                <Fact label="Resolved">
                  {p.outcome.resolved_at ? <RelTime iso={p.outcome.resolved_at} /> : "inside the horizon"}
                </Fact>
                <Fact label="Horizon · hurdle">
                  {p.outcome.horizon_bars} bars · {p.outcome.hurdle_pct}%
                </Fact>
              </dl>
            ) : (
              <p className="muted small">Not scorable yet: there is no price history after it.</p>
            )}
            {p.outcome?.reason && <p className="muted small">{p.outcome.reason}</p>}
          </section>

          {p.execution && (
            <section>
              <h3>Execution</h3>
              <dl className="facts">
                <Fact label="Tranches logged">{p.execution.tranches}</Fact>
                <Fact label="Filled">{p.execution.filled_quantity}</Fact>
                <Fact label="Last state">{p.execution.state ?? DASH}</Fact>
                {p.execution.overridden && <Fact label="Override">a human overrode the risk engine</Fact>}
              </dl>
            </section>
          )}

          <section>
            <h3>In the terminal</h3>
            <ul className="commands">
              <li>
                <code>rhca audit --proposal-id {p.proposal_id}</code>{" "}
                <Copy text={`rhca audit --proposal-id ${p.proposal_id}`} />
              </li>
              {p.actionable && (
                <li>
                  <code>rhca plan-order {p.proposal_id}</code>{" "}
                  <Copy text={`rhca plan-order ${p.proposal_id}`} />
                </li>
              )}
            </ul>
          </section>
        </div>
      )}
    </dialog>
  );
}
