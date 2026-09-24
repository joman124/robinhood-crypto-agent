"use client";

import { useState } from "react";

import { STATUS, statusLabel } from "@/lib/format";
import type { Stats } from "@/lib/types";

type Group = { key: string; title: string; groups: Record<string, Stats> | undefined; label?: (k: string) => string };

/**
 * Win/loss/flat composition per group, one grouping at a time.
 *
 * A stacked bar is the right form: the parts are shares of one whole (every
 * resolved proposal is exactly one of the three), and the comparison that
 * matters is between groups. Segments sit on a 2px surface gap, and every row
 * is direct-labeled so the numbers never depend on reading a color.
 *
 * A grouping with a single row has nothing to compare, so it collapses to one
 * line of text instead of a lone bar.
 */
export function AccuracyBars({ stats }: { stats: {
  by_status?: Record<string, Stats>;
  by_regime: Record<string, Stats>;
  by_symbol: Record<string, Stats>;
  by_side: Record<string, Stats>;
} }) {
  const all: Group[] = [
    { key: "status", title: "Pipeline stage", groups: stats.by_status, label: statusLabel },
    { key: "regime", title: "Regime", groups: stats.by_regime },
    { key: "pair", title: "Pair", groups: stats.by_symbol },
    { key: "side", title: "Side", groups: stats.by_side, label: (k) => k.toUpperCase() },
  ];
  const tabs = all.filter((g) => g.groups && Object.values(g.groups).some((s) => s.resolved > 0));
  const [active, setActive] = useState(tabs[0]?.key);

  if (tabs.length === 0) return null;
  const group = tabs.find((t) => t.key === active) ?? tabs[0];

  const order = group.key === "status" ? Object.keys(STATUS) : [];
  const rows = Object.entries(group.groups ?? {})
    .filter(([, s]) => s.resolved > 0)
    .sort((a, b) =>
      order.length ? order.indexOf(a[0]) - order.indexOf(b[0]) : b[1].resolved - a[1].resolved,
    );
  const name = group.label ?? ((k: string) => k);

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Accuracy by</h2>
        <div className="segmented" role="tablist" aria-label="Group accuracy by">
          {tabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={t.key === group.key}
              className={t.key === group.key ? "on" : ""}
              onClick={() => setActive(t.key)}
            >
              {t.title}
            </button>
          ))}
        </div>
      </div>

      {group.key === "status" && (
        <p className="muted small">
          The shadow run&apos;s question: does each stage earn its place? Compare{" "}
          <em>Proposed</em> against <em>Passed by System 2</em>, and escalated stages against{" "}
          <em>Held back</em>.
        </p>
      )}

      {rows.length === 1 ? (
        <p className="single-row">
          Only one {group.title.toLowerCase()} has resolved outcomes so far:{" "}
          <strong>{name(rows[0][0])}</strong>, {summary(rows[0][1])}.
        </p>
      ) : (
        rows.map(([key, s]) => {
          const total = Math.max(s.resolved, 1);
          const width = (n: number) => `${(n / total) * 100}%`;
          return (
            <div className="bar-row" key={key}>
              <div className="bar-name" title={STATUS[key]?.help}>
                {name(key)}
              </div>
              <div
                className="bar-track"
                role="img"
                aria-label={`${name(key)}: ${s.wins} win, ${s.losses} loss, ${s.flat} flat`}
              >
                {s.wins > 0 && <div className="bar-seg win" style={{ width: width(s.wins) }} title={`${s.wins} win`} />}
                {s.losses > 0 && <div className="bar-seg loss" style={{ width: width(s.losses) }} title={`${s.losses} loss`} />}
                {s.flat > 0 && <div className="bar-seg flat" style={{ width: width(s.flat) }} title={`${s.flat} flat`} />}
              </div>
              <div className="bar-meta">{summary(s)}</div>
            </div>
          );
        })
      )}

      <div className="legend">
        <span>
          <i className="swatch win" aria-hidden="true" /> Win: past the hurdle in the proposal&apos;s
          favour
        </span>
        <span>
          <i className="swatch loss" aria-hidden="true" /> Loss: moved against it
        </span>
        <span>
          <i className="swatch flat" aria-hidden="true" /> Flat: inside the hurdle
        </span>
      </div>
    </section>
  );
}

function summary(s: Stats): string {
  if (s.win_rate === null) return "no evidence yet";
  return `${(s.win_rate * 100).toFixed(0)}% · ${s.wins}W ${s.losses}L ${s.flat}F`;
}
