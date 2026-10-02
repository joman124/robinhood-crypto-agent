"use client";

import { useMemo, useState } from "react";

import { CountUp, useInView } from "@/components/motion";
import {
  FATES,
  FATE_ORDER,
  METRICS,
  type Metric,
  SEGMENTS,
  SEGMENT_ORDER,
  type Fated,
  type Tally,
  rateOf,
  tally,
} from "@/lib/flow";

const R = 46;
const C = 2 * Math.PI * R;

type Grouping = "segment" | "coin";

/**
 * One ring per segment, one success rate at a time.
 *
 * Small multiples, not one chart: each ring is its own share of its own
 * whole, so they sit side by side at one scale and the eye compares arcs.
 * The ring wears the segment's identity color; the fate strip under it wears
 * status colors with their glyphs, so state never rests on hue.
 */
export function SegmentRings({ rows, watchlist }: { rows: Fated[]; watchlist: string[] }) {
  const [metric, setMetric] = useState<Metric>("cleared");
  const [grouping, setGrouping] = useState<Grouping>("segment");
  const [ref, seen] = useInView<HTMLElement>();

  const groups = useMemo(() => {
    if (grouping === "segment") {
      const t = tally(rows, (r) => r.segment);
      return SEGMENT_ORDER.filter((k) => t.has(k)).map((k) => ({
        key: k,
        label: SEGMENTS[k].label,
        slot: SEGMENTS[k].slot,
        t: t.get(k)!,
      }));
    }
    const t = tally(rows, (r) => r.p.symbol);
    const order = [...new Set([...watchlist, ...t.keys()])].filter((s) => t.has(s));
    return order.map((s, i) => ({ key: s, label: s.replace("-USD", ""), slot: i + 1, t: t.get(s)! }));
  }, [rows, grouping, watchlist]);

  if (groups.length === 0) return null;

  return (
    <section className="panel reveal" ref={ref}>
      <div className="panel-head">
        <h2 title={`${METRICS[metric].label}: the share ${METRICS[metric].of}`}>Success rate</h2>
        <div className="head-controls">
          <div className="segmented" role="tablist" aria-label="Success measure">
            {(Object.keys(METRICS) as Metric[]).map((m) => (
              <button key={m} role="tab" aria-selected={m === metric} className={m === metric ? "on" : ""} onClick={() => setMetric(m)}>
                {METRICS[m].label}
              </button>
            ))}
          </div>
          <div className="segmented" role="tablist" aria-label="Group by">
            {(["segment", "coin"] as Grouping[]).map((g) => (
              <button key={g} role="tab" aria-selected={g === grouping} className={g === grouping ? "on" : ""} onClick={() => setGrouping(g)}>
                By {g}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="rings">
        {groups.map(({ key, ...g }, i) => (
          <Ring key={key} {...g} metric={metric} play={seen} delay={i * 120} />
        ))}
      </div>

      <div className="legend">
        {FATE_ORDER.map((f) => (
          <span key={f} title={FATES[f].help}>
            <i className={`fate-glyph fate-${f}`} aria-hidden="true">
              {FATES[f].glyph}
            </i>{" "}
            {FATES[f].label}
          </span>
        ))}
      </div>
    </section>
  );
}

function Ring({
  label,
  slot,
  t,
  metric,
  play,
  delay,
}: {
  label: string;
  slot: number;
  t: Tally;
  metric: Metric;
  play: boolean;
  delay: number;
}) {
  const { hits, of, rate } = rateOf(metric, t);
  const shown = play && rate !== null ? rate : 0;
  const empty = metric === "won" && of === 0 ? "not scored" : "no evidence yet";

  return (
    <figure className="ring-card" style={{ ["--series" as string]: `var(--series-${slot})`, ["--delay" as string]: `${delay}ms` }}>
      <div className="ring-wrap">
        <svg viewBox="0 0 120 120" className="ring" role="img" aria-label={`${label}: ${rate === null ? empty : `${Math.round(rate * 100)}%, ${hits} of ${of}`}`}>
          <circle cx="60" cy="60" r={R} className="ring-track" />
          {rate !== null && (
            <circle
              cx="60"
              cy="60"
              r={R}
              className="ring-arc"
              strokeDasharray={C}
              strokeDashoffset={C * (1 - shown)}
              transform="rotate(-90 60 60)"
            />
          )}
          {rate !== null && play && (
            <circle cx="60" cy="60" r={R} className="ring-glow" strokeDasharray={`2 ${C}`} strokeDashoffset={-C * shown + 1} transform="rotate(-90 60 60)" />
          )}
        </svg>
        <div className="ring-center">
          {rate === null ? (
            <span className="ring-value unknown">—</span>
          ) : (
            <span className="ring-value">
              <CountUp text={`${Math.round(rate * 100)}%`} />
            </span>
          )}
          <span className="ring-sub">{rate === null ? empty : `${hits} of ${of}`}</span>
        </div>
      </div>
      <figcaption>
        <strong>{label}</strong>
        <span className="muted small">
          {t.total}
        </span>
      </figcaption>
      <div className="fate-strip" role="img" aria-label={FATE_ORDER.filter((f) => t.fates[f]).map((f) => `${t.fates[f]} ${FATES[f].label.toLowerCase()}`).join(", ")}>
        {FATE_ORDER.filter((f) => t.fates[f] > 0).map((f) => (
          <i key={f} className={`fate-seg fate-${f}`} style={{ flexGrow: t.fates[f] }} title={`${t.fates[f]} ${FATES[f].label.toLowerCase()}`} />
        ))}
      </div>
    </figure>
  );
}
