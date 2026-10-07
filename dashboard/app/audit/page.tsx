import { AuditLog } from "@/components/AuditLog";
import { RelTime } from "@/components/Live";
import { CountUp } from "@/components/motion";
import { Shell } from "@/components/Shell";
import { SignInForm } from "@/components/SignIn";
import { Badge, Fact, Facts, PageHead } from "@/components/ui";
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

      <section className="card hero-count">
        <div>
          <p className="hero-count-value"><CountUp text={events.length.toLocaleString("en-US")} /></p>
          <p className="muted small">events{earliest ? <> since <RelTime iso={earliest} /></> : ""}</p>
        </div>
        <Facts cols={2}>
          <Fact label="Latest">{latest ? <RelTime iso={latest.at} /> : "—"}</Fact>
          <Fact label="Decisions">{c.decisions.length}</Fact>
        </Facts>
        <Badge tone={c.storage === "kv" ? "mint" : "amber"} icon="link">{c.storage === "kv" ? "Durable" : "Memory only"}</Badge>
      </section>

      {events.length === 0 ? (
        <section className="card empty-state"><h2>No events yet</h2></section>
      ) : (
        <AuditLog events={events} />
      )}
    </Shell>
  );
}
