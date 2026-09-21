import type { Stats } from "@/lib/types";

/**
 * Win/loss/flat composition per group.
 *
 * A stacked bar is the right form here: the parts are shares of one whole
 * (every resolved proposal is exactly one of the three), and the comparison
 * that matters is between groups. Segments are separated by a 2px surface gap
 * so adjacent fills never blur into one another, and every row is direct-labeled
 * so the numbers never depend on reading a color.
 */
export function AccuracyBars({
  title,
  groups,
}: {
  title: string;
  groups: Record<string, Stats>;
}) {
  const rows = Object.entries(groups)
    .filter(([, s]) => s.resolved > 0)
    .sort((a, b) => b[1].resolved - a[1].resolved);

  if (rows.length === 0) return null;

  return (
    <section className="panel">
      <h2>{title}</h2>
      {rows.map(([name, stats]) => {
        const total = Math.max(stats.resolved, 1);
        const pct = (n: number) => `${(n / total) * 100}%`;
        return (
          <div className="bar-row" key={name}>
            <div>{name}</div>
            <div
              className="bar-track"
              role="img"
              aria-label={`${name}: ${stats.wins} win, ${stats.losses} loss, ${stats.flat} flat`}
            >
              {stats.wins > 0 && <div className="bar-seg win" style={{ width: pct(stats.wins) }} />}
              {stats.losses > 0 && (
                <div className="bar-seg loss" style={{ width: pct(stats.losses) }} />
              )}
              {stats.flat > 0 && <div className="bar-seg flat" style={{ width: pct(stats.flat) }} />}
            </div>
            <div className="bar-meta">
              {stats.win_rate === null
                ? "no evidence"
                : `${(stats.win_rate * 100).toFixed(0)}% · ${stats.wins}W/${stats.losses}L/${stats.flat}F`}
            </div>
          </div>
        );
      })}
      <div className="legend">
        <span>
          <i className="swatch win" aria-hidden="true" /> Win — moved past the hurdle in the
          proposal&apos;s favour
        </span>
        <span>
          <i className="swatch loss" aria-hidden="true" /> Loss — moved against it
        </span>
        <span>
          <i className="swatch flat" aria-hidden="true" /> Flat — inside the hurdle
        </span>
      </div>
    </section>
  );
}
