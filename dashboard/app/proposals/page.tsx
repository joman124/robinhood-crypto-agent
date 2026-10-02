import Link from "next/link";

import { Airlock } from "@/components/Airlock";
import { CopyButton } from "@/components/CopyButton";
import { RelTime } from "@/components/Live";
import { ProposalQueue, type QueueTab } from "@/components/ProposalQueue";
import { RiskChecks } from "@/components/RiskChecks";
import { Shell } from "@/components/Shell";
import { SignInForm } from "@/components/SignIn";
import { Badge, CardHead, CoinMark, Fact, FateBadge, Facts, Icon, PageHead } from "@/components/ui";
import { StatusChip, VerdictChip } from "@/components/Verdict";
import { loadConsole } from "@/lib/console";
import { DASH, acceptBlocker, coinOf, isLadder, ladderLabel, money, pct, price, score, signedPct } from "@/lib/format";
import type { Payload, Proposal } from "@/lib/types";

export const dynamic = "force-dynamic";

const ID = /^[0-9a-f]{6,64}$/;
const TABS: QueueTab[] = ["awaiting", "decided", "all"];

export default async function Proposals({ searchParams }: { searchParams: Promise<{ id?: string; tab?: string }> }) {
  const c = await loadConsole();
  if (c.gated) return <SignInForm />;

  const params = await searchParams;
  const { payload, rows, decisions } = c;
  const decided = new Map(decisions.map((d) => [d.proposal_id, d]));
  const awaiting = rows.filter((r) => r.fate === "awaiting");
  const askedFor = params.id && ID.test(params.id) ? params.id : null;
  const tab = TABS.find((t) => t === params.tab) ?? (awaiting.length ? "awaiting" : "all");
  // With no id asked for, open the oldest proposal waiting on you.
  const selected = askedFor ? rows.find((r) => r.p.proposal_id === askedFor) : awaiting.at(-1);

  return (
    <Shell c={c}>
      <PageHead title="Proposals" />

      {rows.length === 0 ? (
        <section className="card empty-state">
          <h2>No proposals yet</h2>
        </section>
      ) : (
        <div className="grid-review">
          <ProposalQueue rows={rows} decidedIds={[...decided.keys()]} selectedId={selected?.p.proposal_id ?? null} initialTab={tab} />
          <div className="stack">
            {selected ? (
              <Review p={selected.p} fate={selected.fate} payload={payload} canDecide={c.mayDecide} decision={decided.get(selected.p.proposal_id)} />
            ) : (
              <section className="card empty-state">
                <Badge tone="mint" icon="circle-check">Queue clear</Badge>
                {askedFor && <h2>Not in the last sync</h2>}
              </section>
            )}
          </div>
        </div>
      )}
    </Shell>
  );
}

function Review({ p, fate, payload, canDecide, decision }: {
  p: Proposal;
  fate: Parameters<typeof FateBadge>[0]["fate"];
  payload: Payload | null;
  canDecide: boolean;
  decision: Parameters<typeof Airlock>[0]["decision"];
}) {
  const blocker = acceptBlocker(p, payload);
  const mark = payload?.market?.[p.symbol];
  const tolerance = payload?.limits?.price_drift_tolerance_pct;
  const drift = p.drift_pct != null ? Number(p.drift_pct) : null;
  const driftOk = drift === null || !tolerance || drift <= Number(tolerance);
  const findings = p.risk_findings ?? [];
  const failed = findings.filter((f) => !f.passed);
  const ladder = isLadder(p);
  const kill = payload?.kill_switch;
  const step = decision ? 3 : p.risk_passed === false ? 2 : 3;

  return (
    <>
      <section className="card">
        <div className="card-head">
          <h2 className="with-icon"><Icon name="lock-keyhole" size={18} /> Approval</h2>
        </div>
        <ol className="steps">
          <li className="done"><span><Icon name="check" size={12} /></span>Review</li>
          <li className={p.risk_passed === false ? "fail" : "done"}>
            <span><Icon name={p.risk_passed === false ? "x" : "check"} size={12} /></span>Risk checks
          </li>
          <li className={decision ? "done" : step === 3 ? "current" : ""}>
            <span>{decision ? <Icon name="check" size={12} /> : "03"}</span>Decide
          </li>
        </ol>
      </section>

      <section className="card">
        <CardHead title={`${coinOf(p.symbol)} · ${ladder ? ladderLabel(p) : "proposal"}`} />
        <div className="pair-row">
          <span className="asset big">
            <CoinMark symbol={p.symbol} size={40} />
            <span>
              <strong>{coinOf(p.symbol)} / USD</strong>
              <small className="eyebrow mint">{p.sleeve ? `${p.sleeve} sleeve` : p.strategy ?? "System 1"} · {p.side === "buy" ? "long" : "exit"}</small>
            </span>
          </span>
          <span className="pair-id">
            <strong className="mono">{p.proposal_id}</strong>
            <CopyButton text={p.proposal_id} />
          </span>
        </div>
        <Facts cols={5}>
          <Fact label="Side" tone={p.side === "buy" ? "mint" : "red"}>{p.side.toUpperCase()}</Fact>
          <Fact label="Notional">{money(p.notional)}</Fact>
          <Fact label="Reference">{price(p.reference_price)}</Fact>
          <Fact label="Quantity">{p.quantity ?? DASH}</Fact>
          <Fact label="Status"><FateBadge fate={fate} /></Fact>
        </Facts>
        <div className="thesis">
          {(p.trigger_reason || p.plan?.rationale) && <p>{p.trigger_reason || p.plan?.rationale}</p>}
          <Facts cols={4}>
            <Fact label="Stage"><StatusChip status={p.status} /></Fact>
            <Fact label="Plan">{p.plan ? `${p.plan.style ?? "prompt"} · ${p.plan.tranches} tranche${p.plan.tranches === 1 ? "" : "s"}` : DASH}</Fact>
            <Fact label="Proposed"><RelTime iso={p.proposed_at} /></Fact>
            {p.score != null ? (
              <Fact label="Composite">{score(p.score)} · {pct(p.confidence)}</Fact>
            ) : (
              <Fact label="Rule">{ladder ? ladderLabel(p) : DASH}</Fact>
            )}
          </Facts>
        </div>
        {(p.signals ?? []).length > 0 && (
          <ul className="signals" aria-label="Signals">
            {p.signals!.map((s) => {
              const v = Math.max(-1, Math.min(1, s.score ?? 0));
              return (
                <li key={s.name} className="signal">
                  <span className="signal-name">{s.name.replace(/_/g, " ")}</span>
                  <span className="signal-track" aria-hidden="true">
                    <span className={v >= 0 ? "signal-bar long" : "signal-bar short"} style={v >= 0 ? { left: "50%", width: `${v * 50}%` } : { right: "50%", width: `${-v * 50}%` }} />
                  </span>
                  <span className="signal-score mono">{score(s.score)}</span>
                </li>
              );
            })}
          </ul>
        )}
        {!ladder && p.system2_decision && (
          <p className="muted small">
            System 2: {p.system2_decision}{p.system2_confidence != null ? ` · ${pct(p.system2_confidence)}` : ""}
          </p>
        )}
      </section>

      <section className="card">
        <CardHead title="Price drift">
          <Badge tone={driftOk ? "mint" : "red"}>{drift === null ? "No mark" : driftOk ? "Within limit" : "Past limit"}</Badge>
        </CardHead>
        <Facts cols={4}>
          <Fact label="Proposed at">{price(p.reference_price)}</Fact>
          <Fact label="Last mark">{mark ? price(mark.mark) : DASH}</Fact>
          <Fact label="Drift" tone={driftOk ? "mint" : "red"}>{drift === null ? DASH : `${drift.toFixed(2)}%`}</Fact>
          <Fact label="Limit">≤ {tolerance ?? DASH}%</Fact>
        </Facts>
      </section>

      <section className="card">
        <CardHead title={findings.length ? `${findings.length - failed.length} / ${findings.length} checks passed` : "Risk checks"}>
          <Badge tone={p.risk_passed === false ? "red" : "mint"}>{p.risk_passed === false ? "Blocked" : "Cleared"}</Badge>
        </CardHead>
        <RiskChecks p={p} />
        <Link className="small" href={`/risk?id=${p.proposal_id}`}>Risk engine →</Link>
      </section>

      <Airlock
        proposal={p}
        decision={decision}
        canDecide={canDecide}
        blocker={blocker}
        syncedAt={payload?.generated_at ?? null}
        checks={[
          { label: "Risk checks", detail: p.risk_passed === false ? `${failed.length || 1} blocking` : "", ok: p.risk_passed !== false },
          { label: "Kill switch", detail: kill?.engaged ? "Engaged" : "", ok: !kill?.engaged },
          { label: "Drift", detail: drift === null ? "No mark" : `${drift.toFixed(2)}% / ${tolerance ?? "?"}%`, ok: driftOk },
        ]}
      />

      <section className="card">
          <CardHead title="Outcome" />
          {p.outcome ? (
            <Facts cols={2}>
              <Fact label="Verdict"><VerdictChip verdict={p.outcome.verdict} /></Fact>
              <Fact label="Move in its favour">{signedPct(p.outcome.signed_move_pct)}</Fact>
              <Fact label="Price">{price(p.outcome.reference_price)} → {price(p.outcome.resolved_price)}</Fact>
              <Fact label="Horizon · hurdle">{p.outcome.horizon_bars} bars · {p.outcome.hurdle_pct}%</Fact>
            </Facts>
          ) : (
            <p className="muted small">{ladder ? "Not scored" : "Pending"}</p>
          )}
          {p.execution && (
            <Facts cols={3}>
              <Fact label="Tranches logged">{p.execution.tranches}</Fact>
              <Fact label="Filled">{p.execution.filled_quantity}</Fact>
              <Fact label="Last state">{p.execution.overridden ? "overridden by a human" : p.execution.state ?? DASH}</Fact>
            </Facts>
          )}
      </section>
    </>
  );
}
