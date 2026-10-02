import Link from "next/link";

import { RelTime } from "@/components/Live";
import { ProposalPicker } from "@/components/ProposalPicker";
import { RiskChecks } from "@/components/RiskChecks";
import { Shell } from "@/components/Shell";
import { SignInForm } from "@/components/SignIn";
import { Badge, CardHead, Fact, Facts, PageHead } from "@/components/ui";
import { loadConsole } from "@/lib/console";
import { DASH, RULES, coinOf, isLadder, ladderLabel, money, price, ruleLabel } from "@/lib/format";
import type { Proposal } from "@/lib/types";

export const dynamic = "force-dynamic";

const ID = /^[0-9a-f]{6,64}$/;
type View = "checks" | "blockers" | "history";
const VIEWS: View[] = ["checks", "blockers", "history"];

export default async function RiskEngine({ searchParams }: { searchParams: Promise<{ id?: string; view?: string }> }) {
  const c = await loadConsole();
  if (c.gated) return <SignInForm />;

  const params = await searchParams;
  const proposals = c.payload?.proposals ?? [];
  const asked = params.id && ID.test(params.id) ? proposals.find((p) => p.proposal_id === params.id) : undefined;
  // Default to the newest proposal the engine blocked: that is where it did its job.
  const p = asked ?? proposals.find((x) => x.risk_passed === false) ?? proposals[0];
  const view = VIEWS.find((v) => v === params.view) ?? "checks";
  const failedCount = p ? (p.risk_findings ?? []).filter((f) => !f.passed).length || (p.risk_failures ?? []).length : 0;
  const href = (v: View) => `/risk?view=${v}${p ? `&id=${p.proposal_id}` : ""}`;

  return (
    <Shell c={c}>
      <PageHead title="Risk Engine">
        {proposals.length > 0 && <ProposalPicker proposals={proposals} selected={p?.proposal_id} />}
      </PageHead>

      <div className="tabs-row">
        <nav className="pills" aria-label="Risk views">
          <Link className={view === "checks" ? "pill on" : "pill"} href={href("checks")}>All checks</Link>
          <Link className={view === "blockers" ? "pill on" : "pill"} href={href("blockers")}>Blockers · {failedCount}</Link>
          <Link className={view === "history" ? "pill on" : "pill"} href={href("history")}>Rule history</Link>
        </nav>
        {p && (
          <div className="badges">
            <Badge tone="amber">Candidate: {coinOf(p.symbol)} {isLadder(p) ? ladderLabel(p) : ""}</Badge>
            <Badge tone="red">{proposals.filter((x) => x.risk_passed === false).length} blocked overall</Badge>
          </div>
        )}
      </div>

      {!p ? (
        <section className="card empty-state"><h2>No evaluations yet</h2></section>
      ) : view === "history" ? (
        <RuleHistory proposals={proposals} />
      ) : (
        <Evaluation p={p} onlyFailed={view === "blockers"} />
      )}
    </Shell>
  );
}

function Evaluation({ p, onlyFailed }: { p: Proposal; onlyFailed: boolean }) {
  const findings = p.risk_findings ?? [];
  const failed = findings.filter((f) => !f.passed);
  const blocked = p.risk_passed === false;
  const first = failed[0];
  const firstRule = first?.rule ?? (p.risk_failures ?? [])[0];
  const passed = findings.length - failed.length;

  return (
    <>
      <div className="grid-side">
        <section className={`card verdict ${blocked ? "blocked" : "cleared"}`}>
          <div className="card-top">
            <Badge tone={blocked ? "red" : "mint"} icon={blocked ? "octagon-x" : "circle-check"}>{blocked ? "Blocked" : "Cleared"}</Badge>
            <span className="eyebrow"><RelTime iso={p.proposed_at} /></span>
          </div>
          <h2>{blocked ? (firstRule ? ruleLabel(firstRule) : "Blocked") : "Cleared every rule"}</h2>
        </section>

        <section className="card">
          <CardHead title={`${coinOf(p.symbol)} · ${isLadder(p) ? ladderLabel(p) : "System 1 candidate"}`}>
            <Badge><span className="mono">{p.proposal_id}</span></Badge>
          </CardHead>
          <Facts cols={5}>
            <Fact label="Notional">{money(p.notional)}</Fact>
            <Fact label="Reference">{price(p.reference_price)}</Fact>
            <Fact label="Drift at sync">{p.drift_pct != null ? `${Number(p.drift_pct).toFixed(2)}%` : DASH}</Fact>
            <Fact label="Side">{p.side.toUpperCase()}</Fact>
            <Fact label="Result" tone={blocked ? "red" : "mint"}>
              {findings.length ? `${passed} pass · ${failed.length} block` : blocked ? "Blocked" : "Passed"}
            </Fact>
          </Facts>
        </section>
      </div>

      <div className="grid-side reverse">
        <section className="card">
          <CardHead title="Checks">
            {findings.length > 0 && <span className="eyebrow"><span className="tone-text mint">{passed} pass</span> · <span className="tone-text red">{failed.length} block</span></span>}
          </CardHead>
          <RiskChecks p={p} onlyFailed={onlyFailed} />
        </section>

        <div className="stack">
          {blocked && firstRule && (
            <section className="card">
              <CardHead title={ruleLabel(firstRule)}>
                <Badge tone="red">{first?.blocking === false ? "Warning" : "Hard limit"}</Badge>
              </CardHead>
              {first?.message && <div className="expression mono">{first.message}<strong className="tone-text red"> → BLOCK</strong></div>}
            </section>
          )}
        </div>
      </div>
    </>
  );
}

/** How often each rule has blocked, across every synced proposal. */
function RuleHistory({ proposals }: { proposals: Proposal[] }) {
  const tally = new Map<string, { fails: number; seen: number; last: string | null }>();
  for (const p of proposals) {
    const failing = new Set([...(p.risk_failures ?? []), ...(p.risk_findings ?? []).filter((f) => !f.passed).map((f) => f.rule)]);
    const rules = new Set([...(p.risk_findings ?? []).map((f) => f.rule), ...failing]);
    for (const rule of rules) {
      const t = tally.get(rule) ?? { fails: 0, seen: 0, last: null };
      t.seen++;
      if (failing.has(rule)) {
        t.fails++;
        if (!t.last || (p.proposed_at && p.proposed_at > t.last)) t.last = p.proposed_at;
      }
      tally.set(rule, t);
    }
  }
  const ranked = [...tally.entries()].sort((a, b) => b[1].fails - a[1].fails || a[0].localeCompare(b[0]));
  const max = Math.max(1, ...ranked.map(([, t]) => t.fails));

  return (
    <section className="card flush">
      <CardHead title="Blocks by rule">
        <span className="eyebrow">{proposals.length} proposals</span>
      </CardHead>
      {ranked.length === 0 ? (
        <p className="muted small pad">—</p>
      ) : (
        <div className="table-scroll">
          <table className="grid-table">
            <thead><tr><th>Rule</th><th>Checks</th><th className="num">Blocked</th><th>Share</th><th>Last blocked</th></tr></thead>
            <tbody>
              {ranked.map(([rule, t]) => (
                <tr key={rule}>
                  <td title={RULES[rule]}><strong>{ruleLabel(rule)}</strong></td>
                  <td className="mono muted">{t.seen}</td>
                  <td className={`num mono ${t.fails ? "tone-text red" : "muted"}`}>{t.fails}</td>
                  <td><span className="meter"><span className="meter-fill red" style={{ width: `${(t.fails / max) * 100}%` }} /></span></td>
                  <td className="muted small">{t.last ? <RelTime iso={t.last} /> : "never"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
