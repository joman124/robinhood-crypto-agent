import { RelTime } from "@/components/Live";
import type { Heartbeat } from "@/lib/types";

const SERVICES: [string, string][] = [
  ["robinhood", "Robinhood quotes"],
  ["jev", "Jev news labels"],
  ["system2", "System 2 (Sonnet 5)"],
  ["market_data", "Crypto.com market data"],
  ["dashboard", "Dashboard sync"],
];

const COUNTS: [string, string][] = [
  ["quotes", "quote polls"],
  ["news", "headlines"],
  ["labeled", "labeled by Jev"],
  ["candidates", "candidates"],
  ["escalations", "escalated"],
  ["proposed", "proposed"],
  ["errors", "errors"],
];

/** What `rhca status` prints about the shadow loop, as of the last sync. */
export function Pipeline({ beat }: { beat: Heartbeat | null | undefined }) {
  if (beat === undefined) return null;

  if (beat === null) {
    return (
      <section className="panel">
        <h2>Shadow loop</h2>
        <p className="muted">
          No heartbeat yet. Start it where the agent lives with <code>rhca run</code>.
        </p>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Shadow loop</h2>
        <span className="muted small">
          {beat.mode ?? "shadow"} mode · {beat.cycles ?? 0} cycles since{" "}
          <RelTime iso={beat.started_at} />
        </span>
      </div>

      <div className="counts">
        {COUNTS.map(([key, label]) => (
          <div key={key} className={key === "errors" && (beat.counts[key] ?? 0) > 0 ? "count bad" : "count"}>
            <div className="count-value">{beat.counts[key] ?? 0}</div>
            <div className="count-label">{label}</div>
          </div>
        ))}
      </div>
      <p className="muted small">
        Counts are for the current run. {beat.escalations_today ?? 0} escalation(s) to System 2
        today.
      </p>

      <ul className="services" aria-label="Services">
        {SERVICES.map(([key, label]) => {
          const on = Boolean(beat.services[key]);
          return (
            <li key={key} className={on ? "on" : "off"}>
              <span aria-hidden="true">{on ? "✓" : "–"}</span> {label}
              <span className="sr-only">{on ? " enabled" : " off"}</span>
            </li>
          );
        })}
      </ul>

      {beat.last_error && (
        <p className="alert warn compact">
          <strong>Last error</strong> in <code>{beat.last_error.task ?? "a task"}</code>,{" "}
          <RelTime iso={beat.last_error.at} />. The text stays on the agent&apos;s machine; read it
          with <code>rhca status</code>.
        </p>
      )}
    </section>
  );
}
