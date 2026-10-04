import Link from "next/link";

import { CsvButton } from "@/components/CsvButton";
import { RelTime } from "@/components/Live";
import { CountUp } from "@/components/motion";
import { Pipeline } from "@/components/Pipeline";
import { ProfitHero, TakenLedger } from "@/components/Profit";
import { Shell } from "@/components/Shell";
import { SignalClock } from "@/components/ShellClient";
import { SignInForm } from "@/components/SignIn";
import { TradeFlow } from "@/components/TradeFlow";
import { TradeTimeline } from "@/components/TradeTimeline";
import { Badge, CardHead, CoinMark, Fact, FateBadge, Facts, Icon, PageHead } from "@/components/ui";
import { loadConsole } from "@/lib/console";
import { FATES, FATE_ORDER, type Fated, SEGMENTS } from "@/lib/flow";
import { coinOf, isLadder, ladderLabel, money, price } from "@/lib/format";
import { pnlSummary, pnlTone, signedMoney } from "@/lib/pnl";

export const dynamic = "force-dynamic";

export default async function CommandCenter() {
  const c = await loadConsole();
  if (c.gated) return <SignInForm />;

  const { payload, rows } = c;
  const count = (f: string) => rows.filter((r) => r.fate === f).length;
  const blocked = count("blocked");
  const awaiting = rows.filter((r) => r.fate === "awaiting");
  const pnl = pnlSummary(payload);
  const split = rows.filter((r) => r.p.strategy === "split").length;
  const kill = payload?.kill_switch;

  return (
    <Shell c={c}>
      <PageHead title="Command Center">
        <CsvButton proposals={payload?.proposals ?? []} />
        <Link className="btn primary" href="/proposals">
          <Icon name="file-check" /> Review queue
        </Link>
      </PageHead>

      <div className="grid-main">
        <ProfitHero s={pnl} accepted={count("accepted")} />
        <AwaitingCard first={awaiting.at(-1)} total={awaiting.length} tolerance={payload?.limits?.price_drift_tolerance_pct} />
      </div>

      <TakenLedger s={pnl} />

      <section className="card kpi-strip quiet" aria-label="How the algorithm's gates filtered">
        <Kpi label="Proposals" value={String(rows.length)} note={`${split} split · ${rows.length - split} retired`} />
        <Kpi
          label="Cleared risk"
          value={rows.length ? `${Math.round(((rows.length - blocked) / rows.length) * 100)}%` : "—"}
          note={`${blocked} blocked`}
        />
        <Kpi label="Needs your call" value={String(awaiting.length)} note="" tone="amber" />
        <Kpi label="Placed" value={String(count("placed"))} note={`${count("accepted")} accepted`} />
        {rows.length > 0 && <FateBar rows={rows} />}
      </section>

      <Sleeves rows={rows} />

      <div className="grid-main">
        <section className="card flush">
          <CardHead title="Watchlist" />
          <div className="table-scroll">
            <table className="grid-table">
              <thead>
                <tr><th>Asset</th><th className="num">Mark</th><th>Latest proposal</th><th className="num">Realized P&amp;L</th></tr>
              </thead>
              <tbody>
                {(payload?.watchlist ?? []).map((sym) => {
                  const mark = payload?.market?.[sym];
                  const latest = rows.find((r) => r.p.symbol === sym);
                  const pl = payload?.split_realized_pnl?.[sym];
                  return (
                    <tr key={sym}>
                      <td>
                        <span className="asset">
                          <CoinMark symbol={sym} />
                          <span><strong>{coinOf(sym)} / USD</strong><small>{mark ? <RelTime iso={mark.observed_at} /> : "no mark"}</small></span>
                        </span>
                      </td>
                      <td className="num mono">{mark ? price(mark.mark) : "—"}</td>
                      <td>
                        {latest ? (
                          <Link className="row-link" href={`/proposals?id=${latest.p.proposal_id}`}>
                            <FateBadge fate={latest.fate} />
                            <span>{isLadder(latest.p) ? ladderLabel(latest.p) : SEGMENTS[latest.segment].label}</span>
                            <small><RelTime iso={latest.p.proposed_at} /></small>
                          </Link>
                        ) : (
                          <span className="muted small">—</span>
                        )}
                      </td>
                      <td className={`num mono tone-text ${pnlTone(pl ? Number(pl) : null)}`}>{pl ? signedMoney(Number(pl)) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <div className="stack">
          <section className="card">
            <CardHead title="Safety">
              <Badge tone={kill?.engaged ? "red" : "mint"}>{kill?.engaged ? "Halted" : "Nominal"}</Badge>
            </CardHead>
            <dl className="posture">
              <div><dt>Drift limit</dt><dd>≤ {payload?.limits?.price_drift_tolerance_pct ?? "—"}%</dd></div>
              <div><dt>Kill switch</dt><dd className={kill?.engaged ? "tone-text red" : undefined}>{kill ? (kill.engaged ? "Engaged" : "Armed") : "—"}</dd></div>
              <div><dt>Mode</dt><dd className="tone-text violet">{(payload?.execution_mode ?? "propose_only").replace(/_/g, "-")}</dd></div>
            </dl>
          </section>
          <section className="card">
            <CardHead title="Next daily close" />
            <SignalClock />
          </section>
        </div>
      </div>

      {rows.length > 0 && (
        <>
          <TradeFlow rows={rows} />
          <TradeTimeline rows={rows} />
        </>
      )}
      {payload && <Pipeline beat={payload.pipeline} />}
    </Shell>
  );
}

function Kpi({ label, value, note, tone }: { label: string; value: string; note: string; tone?: string }) {
  return (
    <div className="kpi">
      <span className="kpi-label">{label}</span>
      <span className="kpi-value"><CountUp text={value} /></span>
      <span className={`kpi-note ${tone ? `tone-text ${tone}` : ""}`}>{note}</span>
    </div>
  );
}

/** Every proposal by what became of it, as one bar. */
function FateBar({ rows }: { rows: Fated[] }) {
  const n = (f: string) => rows.filter((r) => r.fate === f).length;
  const shown = FATE_ORDER.filter((f) => n(f) > 0);
  return (
    <div className="fate-bar" role="img" aria-label={shown.map((f) => `${n(f)} ${FATES[f].label.toLowerCase()}`).join(", ")}>
      {shown.map((f, i) => (
        <div key={f} className={`fate-bar-seg fate-${f}`} style={{ flexGrow: n(f), animationDelay: `${200 + i * 90}ms` }}>
          <span className="fate-bar-label">{FATES[f].glyph} {FATES[f].short} {n(f)}</span>
        </div>
      ))}
    </div>
  );
}

function Sleeves({ rows }: { rows: Fated[] }) {
  const sleeves = [
    { key: "long", tag: "Long-term" },
    { key: "short", tag: "Breakout" },
  ] as const;
  return (
    <section className="card">
      <CardHead title="Sleeves" />
      <div className="sleeves">
        {sleeves.map((s) => {
          const mine = rows.filter((r) => r.segment === s.key);
          const by = (f: string) => mine.filter((r) => r.fate === f).length;
          const coins = [...new Set(mine.map((r) => r.p.symbol))];
          return (
            <div key={s.key} className={`sleeve ${s.key}`}>
              <p className="eyebrow">{s.tag}</p>
              <p className="sleeve-value"><CountUp text={String(mine.length)} /> <small>proposals</small></p>
              <Facts cols={3}>
                <Fact label="Placed" tone="mint">{by("placed")}</Fact>
                <Fact label="Your call" tone="amber">{by("awaiting")}</Fact>
                <Fact label="Blocked" tone="red">{by("blocked")}</Fact>
              </Facts>
              <div className="coin-bars" aria-label="Proposals by coin">
                {coins.map((sym) => (
                  <span key={sym} className={`coin-bar coin-${coinOf(sym).toLowerCase()}`} style={{ flexGrow: mine.filter((r) => r.p.symbol === sym).length }} title={`${coinOf(sym)}: ${mine.filter((r) => r.p.symbol === sym).length}`} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function AwaitingCard({ first, total, tolerance }: { first: Fated | undefined; total: number; tolerance?: string }) {
  if (!first) {
    return (
      <section className="card awaiting empty-state">
        <Badge tone="mint" icon="circle-check">Queue clear</Badge>
        <Link className="btn" href="/proposals?tab=all">All proposals</Link>
      </section>
    );
  }
  const { p } = first;
  const findings = p.risk_findings ?? [];
  const passed = findings.filter((f) => f.passed).length;
  const drift = Number(p.drift_pct);
  return (
    <section className="card awaiting">
      <div className="card-top">
        <Badge tone="amber" icon="clock">Your call{total > 1 ? ` · 1 of ${total}` : ""}</Badge>
        <span className="eyebrow"><RelTime iso={p.proposed_at} /></span>
      </div>
      <div className="asset big">
        <CoinMark symbol={p.symbol} size={36} />
        <span>
          <strong>{p.side === "buy" ? "Buy" : "Sell"} {coinOf(p.symbol)} · {isLadder(p) ? ladderLabel(p) : "proposal"}</strong>
          <small className="mono tone-text mint">{p.proposal_id}</small>
        </span>
      </div>
      <Facts cols={3}>
        <Fact label="Notional">{money(p.notional)}</Fact>
        <Fact label="Reference">{price(p.reference_price)}</Fact>
        <Fact label="Drift at sync" tone={tolerance && drift > Number(tolerance) ? "red" : undefined}>
          {p.drift_pct != null ? `${drift.toFixed(2)}%` : "—"}
        </Fact>
      </Facts>
      <div className="inline-check">
        <Icon name="shield-check" />
        <span>{findings.length ? `${passed} / ${findings.length} checks passed` : "Risk passed"}</span>
      </div>
      <Link className="btn primary wide" href={`/proposals?id=${p.proposal_id}`}>
        <Icon name="arrow-right" /> Review
      </Link>
    </section>
  );
}
