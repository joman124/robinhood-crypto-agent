import { AuditLog } from "@/components/AuditLog";
import { RelTime } from "@/components/Live";
import { CountUp } from "@/components/motion";
import { Shell } from "@/components/Shell";
import { SignInForm } from "@/components/SignIn";
import { Badge, CardHead, Fact, Facts, PageHead } from "@/components/ui";
import { loadConsole } from "@/lib/console";
import { eventsOf } from "@/lib/events";

export const dynamic = "force-dynamic";

export default async function AuditTrail() {
  const c = await loadConsole();
  if (c.gated) return <SignInForm />;

  const events = eventsOf(c.payload, c.decisions);
  const earliest = events.at(-1)?.at;
  const latest = events[0];

  return (
    <Shell c={c}>
      <PageHead title="Audit Trail" />

      <div className="grid-side">
        <section className="card hero-count">
          <div className="card-top">
            <Badge tone="mint" icon="link">{c.storage === "kv" ? "Durable store" : "Memory only"}</Badge>
            <span className={`dot ${c.storage === "kv" ? "mint" : "amber"}`} aria-hidden="true" />
          </div>
          <p className="hero-count-value"><CountUp text={events.length.toLocaleString("en-US")} /></p>
          <p className="muted small">events{earliest ? <> since <RelTime iso={earliest} /></> : ""}</p>
        </section>
        <section className="card">
          <CardHead title="Record" />
          <Facts cols={5}>
            <Fact label="Latest event">{latest ? <RelTime iso={latest.at} /> : "—"}</Fact>
            <Fact label="Last sync">{c.payload ? <RelTime iso={c.payload.generated_at} /> : "never"}</Fact>
            <Fact label="Decisions recorded">{c.decisions.length}</Fact>
            <Fact label="Storage mode" tone={c.storage === "kv" ? "mint" : "amber"}>{c.storage === "kv" ? "KV" : "MEMORY"}</Fact>
            <Fact label="Kill switch" tone={c.payload?.kill_switch?.engaged ? "red" : undefined}>
              {c.payload?.kill_switch ? (c.payload.kill_switch.engaged ? "Engaged" : "Armed") : "—"}
            </Fact>
          </Facts>
        </section>
      </div>

      {events.length === 0 ? (
        <section className="card empty-state"><h2>No events yet</h2></section>
      ) : (
        <AuditLog events={events} />
      )}
    </Shell>
  );
}
