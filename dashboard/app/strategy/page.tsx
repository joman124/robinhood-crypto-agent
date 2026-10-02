import Link from "next/link";

import { AccuracyBars } from "@/components/AccuracyBars";
import { HitRateTrend } from "@/components/HitRateTrend";
import { RelTime } from "@/components/Live";
import { CountUp } from "@/components/motion";
import { SegmentRings } from "@/components/SegmentRings";
import { Shell } from "@/components/Shell";
import { SignInForm } from "@/components/SignIn";
import { CardHead, CoinMark, FateBadge, Icon, PageHead } from "@/components/ui";
import { loadConsole } from "@/lib/console";
import type { Fated, SegmentKey } from "@/lib/flow";
import { coinOf, ladderLabel, money, pct, price, signedPct } from "@/lib/format";

export const dynamic = "force-dynamic";

const SLEEVES: { key: SegmentKey; title: string }[] = [
  { key: "long", title: "Long-term" },
  { key: "short", title: "Breakout" },
];

export default async function Strategy() {
  const c = await loadConsole();
  if (c.gated) return <SignInForm />;

  const { payload, rows } = c;
  const split = rows.filter((r) => r.p.strategy === "split");
  const pnl = payload?.split_realized_pnl ?? {};
  const pnlEntries = Object.entries(pnl);
  const pnlTotal = pnlEntries.reduce((s, [, v]) => s + Number(v), 0);
  const watchlist = payload?.watchlist ?? [];
  const stats = payload?.stats.overall;
  const doc = `${c.repo}/blob/main/docs/strategy.md`;

  return (
    <Shell c={c}>
      <PageHead title="Strategy">
        <a className="btn" href={doc} target="_blank" rel="noopener noreferrer"><Icon name="book-open" /> Strategy doc</a>
      </PageHead>

      <section className="card kpi-strip" aria-label="The split in numbers">
        <div className="kpi"><span className="kpi-label">Split realized P&amp;L</span><span className={`kpi-value ${pnlTotal < 0 ? "tone-text red" : ""}`}><CountUp text={pnlEntries.length ? money(pnlTotal.toFixed(2)) : "—"} /></span></div>
        <div className="kpi"><span className="kpi-label">Long-term proposals</span><span className="kpi-value"><CountUp text={String(split.filter((r) => r.segment === "long").length)} /></span></div>
        <div className="kpi"><span className="kpi-label">Breakout proposals</span><span className="kpi-value tone-text mint"><CountUp text={String(split.filter((r) => r.segment === "short").length)} /></span></div>
        <div className="kpi"><span className="kpi-label">Placed</span><span className="kpi-value"><CountUp text={String(split.filter((r) => r.fate === "placed").length)} /></span></div>
      </section>

      <section className="card">
        <CardHead title="Sleeves" />
        <div className="sleeves">
          {SLEEVES.map((s) => {
            const mine = split.filter((r) => r.segment === s.key);
            const max = Math.max(1, ...watchlist.map((sym) => mine.filter((r) => r.p.symbol === sym).length));
            return (
              <div key={s.key} className={`sleeve big ${s.key}`}>
                <div className="sleeve-top">
                  <h3>{s.title}</h3>
                  <span className="sleeve-value"><CountUp text={String(mine.length)} /> <small>proposals</small></span>
                </div>
                <ul className="coin-meters">
                  {watchlist.map((sym) => {
                    const n = mine.filter((r) => r.p.symbol === sym).length;
                    const placed = mine.filter((r) => r.p.symbol === sym && r.fate === "placed").length;
                    return (
                      <li key={sym}>
                        <span className="mono">{coinOf(sym)}</span>
                        <span className="meter"><span className={`meter-fill coin-${coinOf(sym).toLowerCase()}`} style={{ width: `${(n / max) * 100}%` }} /></span>
                        <span className="mono muted">{placed}/{n} placed</span>
                      </li>
                    );
                  })}
                </ul>
                <LatestOf rows={mine} />
              </div>
            );
          })}
        </div>
      </section>

      <section className="card flush">
        <CardHead title="By coin" />
        <div className="table-scroll">
          <table className="grid-table">
            <thead>
              <tr><th>Asset</th><th className="num">Mark</th><th>Long-term</th><th>Breakout</th><th className="num">Realized P&amp;L</th><th>Latest</th></tr>
            </thead>
            <tbody>
              {watchlist.map((sym) => {
                const coinRows = split.filter((r) => r.p.symbol === sym);
                const tally = (k: SegmentKey) => {
                  const r = coinRows.filter((x) => x.segment === k);
                  return `${r.filter((x) => x.fate === "placed").length} / ${r.length}`;
                };
                const latest = coinRows[0];
                const mark = payload?.market?.[sym];
                return (
                  <tr key={sym}>
                    <td><span className="asset"><CoinMark symbol={sym} /><strong>{coinOf(sym)}</strong></span></td>
                    <td className="num mono">{mark ? price(mark.mark) : "—"}</td>
                    <td className="mono">{tally("long")} <small className="muted">placed</small></td>
                    <td className="mono tone-text mint">{tally("short")} <small className="muted">placed</small></td>
                    <td className={`num mono ${Number(pnl[sym]) < 0 ? "tone-text red" : pnl[sym] ? "tone-text mint" : "muted"}`}>{pnl[sym] ? money(pnl[sym]) : "—"}</td>
                    <td>{latest ? <Link className="row-link" href={`/proposals?id=${latest.p.proposal_id}`}><FateBadge fate={latest.fate} /><span>{ladderLabel(latest.p)}</span></Link> : <span className="muted small">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {rows.length > 0 && <SegmentRings rows={rows} watchlist={watchlist} />}

      {payload && (
        <>
          <div className="section-title">
            <h2>System 1 (retired)</h2>
          </div>
          {stats && (
            <section className="card kpi-strip three">
              <div className="kpi"><span className="kpi-label">Hit rate</span><span className="kpi-value"><CountUp text={pct(stats.win_rate)} /></span><span className="kpi-note">{stats.win_rate === null ? "" : `${stats.wins}W / ${stats.losses}L · ${stats.flat} flat`}</span></div>
              <div className="kpi"><span className="kpi-label">Average move</span><span className="kpi-value"><CountUp text={stats.average_move_pct !== null ? signedPct(stats.average_move_pct) : "—"} /></span><span className="kpi-note">{stats.best_move_pct !== null ? `best ${signedPct(stats.best_move_pct)} · worst ${signedPct(stats.worst_move_pct)}` : ""}</span></div>
              <div className="kpi"><span className="kpi-label">Scored</span><span className="kpi-value"><CountUp text={String(stats.resolved)} /></span><span className="kpi-note">of {rows.filter((r) => r.segment === "system1").length}</span></div>
            </section>
          )}
          <div className="grid-2">
            <HitRateTrend proposals={payload.proposals} />
            <AccuracyBars stats={payload.stats} />
          </div>
        </>
      )}
    </Shell>
  );
}

function LatestOf({ rows }: { rows: Fated[] }) {
  const latest = rows[0];
  if (!latest) return null;
  return (
    <Link className="latest" href={`/proposals?id=${latest.p.proposal_id}`}>
      <CoinMark symbol={latest.p.symbol} size={26} />
      <span>
        <strong>{coinOf(latest.p.symbol)} · {ladderLabel(latest.p)}</strong>
        <small><RelTime iso={latest.p.proposed_at} /> · {money(latest.p.notional)}</small>
      </span>
      <FateBadge fate={latest.fate} />
    </Link>
  );
}
