import { AccuracyBars } from "@/components/AccuracyBars";
import { ProposalTable } from "@/components/ProposalTable";
import { SignInForm, SignOutButton } from "@/components/SignIn";
import { StatTile } from "@/components/StatTile";
import { canDecide, isAuthenticated, passwordConfigured } from "@/lib/session";
import { getDecisions, getPayload, storageMode } from "@/lib/store";

export const dynamic = "force-dynamic";

const FALLBACK_REPO = "https://github.com/joman124/robinhood-crypto-agent";

function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(0)}%`;
}

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
  const storage = storageMode();

  return (
    <main className="wrap">
      <div className="masthead">
        <h1>Robinhood crypto agent</h1>
        <a className="badge" href={repo} target="_blank" rel="noreferrer">
          ↗ GitHub
        </a>
        {payload && <span className="badge">mode: {payload.execution_mode}</span>}
        {payload && (
          <span className="badge" title={payload.generated_at}>
            synced {new Date(payload.generated_at).toLocaleString()}
          </span>
        )}
        {authed && <SignOutButton />}
      </div>
      <p className="subtitle">
        Every trade the agent has suggested, whether it turned out to be a good call, and
        your sign-off.{" "}
        {scoring && (
          <>
            An outcome is scored {scoring.horizon_bars} bars (
            {scoring.horizon_bars * scoring.bar_interval_minutes / 60}h) after the proposal,
            and only counts as a win past a {scoring.hurdle_pct}% hurdle — roughly the
            round-trip spread.
          </>
        )}
      </p>

      {!passwordConfigured() && (
        <div className="notice">
          <strong>Read-only.</strong> <code>DASHBOARD_PASSWORD</code> is not set, so no
          decision can be recorded here. Set it in your Vercel project to enable
          accept/decline.
        </div>
      )}

      {storage === "memory" && (
        <div className="notice">
          <strong>Using in-process memory.</strong> No <code>KV_REST_API_URL</code> is
          configured, so anything recorded here is lost on the next cold start and is not
          shared between serverless instances. Connect Vercel KV before relying on it.
        </div>
      )}

      <div className="notice">
        <strong>Accepting here does not place an order.</strong> It records your decision.
        The agent picks it up and still runs the kill switch, a fresh price-drift check,
        remaining-quantity accounting and order-contract validation before anything is
        submitted. This page holds no Robinhood credentials.
      </div>

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
            label="Proposals"
            value={String(stats.total)}
            note={`${payload?.watchlist.length ?? 0} pair(s) watched`}
          />
          <StatTile
            label="Resolved"
            value={String(stats.resolved)}
            note={`${stats.pending} still inside the horizon`}
          />
          <StatTile
            label="Average move"
            value={stats.average_move_pct !== null ? `${stats.average_move_pct}%` : "—"}
            unknown={stats.average_move_pct === null}
            note={
              stats.best_move_pct !== null
                ? `best ${stats.best_move_pct}% · worst ${stats.worst_move_pct}%`
                : "in the proposal's favour"
            }
          />
        </div>
      )}

      {payload && (
        <>
          <AccuracyBars title="Accuracy by regime" groups={payload.stats.by_regime} />
          <AccuracyBars title="Accuracy by pair" groups={payload.stats.by_symbol} />
        </>
      )}

      <ProposalTable
        proposals={payload?.proposals ?? []}
        decisions={decisions}
        canDecide={mayDecide}
      />

      <div className="footer">
        <span>
          Hit rate counts wins against wins + losses. Flat outcomes — moves inside the
          hurdle — are excluded, and an unresolved proposal is never counted as a loss.
        </span>
        <a href={repo} target="_blank" rel="noreferrer">
          Source and risk controls on GitHub
        </a>
      </div>
    </main>
  );
}
