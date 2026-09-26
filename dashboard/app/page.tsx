import { AccuracyBars } from "@/components/AccuracyBars";
import { HitRateTrend } from "@/components/HitRateTrend";
import { LiveProvider } from "@/components/Live";
import { Pipeline } from "@/components/Pipeline";
import { ProposalLog } from "@/components/ProposalLog";
import { SignInForm, SignOutButton } from "@/components/SignIn";
import { StatTile } from "@/components/StatTile";
import { StatusAlerts, StatusStrip } from "@/components/StatusStrip";
import { money, pct, signedPct } from "@/lib/format";
import { canDecide, isAuthenticated, passwordConfigured } from "@/lib/session";
import { getDecisions, getPayload, storageMode } from "@/lib/store";

export const dynamic = "force-dynamic";

const FALLBACK_REPO = "https://github.com/joman124/robinhood-crypto-agent";

export default async function Page() {
  const [payload, decisions, authed, mayDecide] = await Promise.all([
    getPayload(),
    getDecisions(),
    isAuthenticated(),
    canDecide(),
  ]);

  // Password set but not signed in: show only the sign-in form. Proposal data
  // is not rendered to an unauthenticated visitor.
  if (passwordConfigured() && !authed) {
    return <SignInForm />;
  }

  const repo = payload?.repo_url || process.env.NEXT_PUBLIC_REPO_URL || FALLBACK_REPO;
  const stats = payload?.stats.overall;
  const scoring = payload?.scoring;
  const today = payload?.today;
  const decidedIds = new Set(decisions.map((d) => d.proposal_id));
  const awaiting = (payload?.proposals ?? []).filter((p) => p.actionable && !decidedIds.has(p.proposal_id));
  const doc = (path: string) => `${repo}/blob/main/${path}`;

  return (
    <LiveProvider serverNow={Date.now()}>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
              <rect width="32" height="32" rx="8" className="brand-mark" />
              <path d="M7 21l6-6 4 4 8-9" className="brand-line" />
            </svg>
            <div>
              <div className="brand-name">Robinhood crypto agent</div>
              <div className="brand-sub">Proposals, their track record, and your sign-off</div>
            </div>
          </div>
          <nav className="topnav">
            {awaiting.length > 0 && (
              <a href="#proposals" className="attention">
                {awaiting.length} need{awaiting.length === 1 ? "s" : ""} your call
              </a>
            )}
            <a href={repo} target="_blank" rel="noreferrer">
              GitHub ↗
            </a>
            {authed && <SignOutButton />}
          </nav>
        </div>
        <div className="topbar-inner">
          <StatusStrip payload={payload} />
        </div>
      </header>

      <main className="wrap">
        <StatusAlerts payload={payload} />

        {!passwordConfigured() && (
          <div className="alert warn">
            <strong>Read-only.</strong> <code>DASHBOARD_PASSWORD</code> is not set, so no decision
            can be recorded here. Set it in the Vercel project to enable accept and decline.
          </div>
        )}
        {storageMode() === "memory" && (
          <div className="alert warn">
            <strong>Using in-process memory.</strong> No <code>KV_REST_API_URL</code> is configured,
            so anything recorded here is lost on the next cold start and is not shared between
            serverless instances. Connect Vercel KV before relying on it.
          </div>
        )}

        {stats && (
          <div className="tiles">
            <StatTile
              label="Hit rate"
              value={pct(stats.win_rate)}
              unknown={stats.win_rate === null}
              note={
                stats.win_rate === null
                  ? "nothing has resolved yet"
                  : `${stats.wins}W / ${stats.losses}L · ${stats.flat} flat`
              }
            />
            <StatTile
              label="Average move"
              value={stats.average_move_pct !== null ? signedPct(stats.average_move_pct) : "—"}
              unknown={stats.average_move_pct === null}
              note={
                stats.best_move_pct !== null
                  ? `best ${signedPct(stats.best_move_pct)} · worst ${signedPct(stats.worst_move_pct)}`
                  : "in the proposal's favour"
              }
            />
            <StatTile
              label="Proposals"
              value={String(stats.total)}
              note={`${stats.resolved} resolved · ${stats.pending} inside the horizon`}
            />
            {today && (
              <StatTile
                label="Today (UTC)"
                value={`${today.proposals}`}
                note={`proposal${today.proposals === 1 ? "" : "s"} · ${today.executions} executed · ${money(today.executed_notional)} traded · P&L ${money(today.realized_pnl)}`}
              />
            )}
          </div>
        )}

        <p className="callout">
          <strong>Accepting here does not place an order.</strong> It records your decision. The
          agent picks it up and still runs the kill switch, a fresh price-drift check,
          remaining-quantity accounting and order validation before anything is submitted. This
          page holds no Robinhood credentials.
        </p>

        <ProposalLog payload={payload} decisions={decisions} canDecide={mayDecide} />

        {payload && (
          <div className="grid-2">
            <HitRateTrend proposals={payload.proposals} />
            <Pipeline beat={payload.pipeline} />
          </div>
        )}

        {payload && <AccuracyBars stats={payload.stats} />}

        <footer className="footer">
          <p>
            {scoring && (
              <>
                An outcome is scored {scoring.horizon_bars} bars (
                {(scoring.horizon_bars * scoring.bar_interval_minutes) / 60}h) after the proposal,
                as the whole round trip: in at the proposal&apos;s price, out at the far side of the
                book. It is a win only if it made more than {scoring.hurdle_pct}% after that, and a
                loss if it lost money at all.{" "}
              </>
            )}
            Hit rate is wins over wins + losses. Flat outcomes are excluded, an unresolved proposal
            is never a loss, and every candidate is scored, including ones you declined and ones the
            pipeline held back.
          </p>
          <nav className="footer-links">
            <a href={doc("docs/risk-controls.md")} target="_blank" rel="noreferrer">
              Risk controls
            </a>
            <a href={doc("docs/strategy.md")} target="_blank" rel="noreferrer">
              Strategy
            </a>
            <a href={doc("docs/runbook.md")} target="_blank" rel="noreferrer">
              Runbook
            </a>
            <a href={doc("docs/autonomy.md")} target="_blank" rel="noreferrer">
              Path to autonomy
            </a>
          </nav>
        </footer>
      </main>
    </LiveProvider>
  );
}
