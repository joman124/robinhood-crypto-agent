import { RelTime } from "@/components/Live";
import type { Heartbeat } from "@/lib/types";

const COUNTS: [string, string][] = [
  ["quotes", "quotes"],
  ["candidates", "proposals"],
  ["proposed", "passed risk"],
  ["errors", "errors"],
];

/** What `rhca status` prints about the shadow loop, as of the last sync. */
export function Pipeline({ beat }: { beat: Heartbeat | null | undefined }) {
  if (beat === undefined) return null;

  if (beat === null) {
    return (
      <section className="panel">
        <h2>Shadow loop</h2>
        <p className="muted">—</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Shadow loop</h2>
        <span className="muted small">
          {beat.cycles ?? 0} cycles · <RelTime iso={beat.last_cycle_at} />
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

      {beat.last_error && (
        <p className="alert warn compact">
          <strong>Last error</strong> · <code>{beat.last_error.task ?? "task"}</code> ·{" "}
          <RelTime iso={beat.last_error.at} />
        </p>
      )}
    </section>
  );
}
