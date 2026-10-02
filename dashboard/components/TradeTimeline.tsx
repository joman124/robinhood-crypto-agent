"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { useInView } from "@/components/motion";
import { isLadder, ladderLabel, money } from "@/lib/format";
import { FATES, FATE_ORDER, type Fate, type Fated, SEGMENTS, SEGMENT_ORDER, type SegmentKey } from "@/lib/flow";

const DOT = 4.5;
const LANE_MIN = 44;
const LANE_MAX = 132;
const PAD = { left: 132, right: 14, top: 8, bottom: 26 };

type Dot = Fated & { x: number; y: number; i: number };

/**
 * Every proposal as a dot on the clock, one lane per segment, shaped and
 * colored by what became of it. Dots settle beeswarm-style so a busy hour
 * stacks instead of hiding itself. Click one to open it.
 */
export function TradeTimeline({ rows }: { rows: Fated[] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [ref, seen] = useInView<HTMLElement>();
  const [width, setWidth] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<Fate>>(new Set());
  const [hover, setHover] = useState<Dot | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(300, entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const timed = useMemo(
    () => rows.filter((r) => r.p.proposed_at && !Number.isNaN(Date.parse(r.p.proposed_at))),
    [rows],
  );
  const counts = useMemo(() => {
    const c = new Map<Fate, number>();
    timed.forEach((r) => c.set(r.fate, (c.get(r.fate) ?? 0) + 1));
    return c;
  }, [timed]);

  const layout = useMemo(() => {
    if (width === null || timed.length === 0) return null;
    const narrow = width < 560;
    const left = narrow ? 12 : PAD.left;
    const times = timed.map((r) => Date.parse(r.p.proposed_at!));
    const t0 = Math.min(...times);
    const t1 = Math.max(...times);
    const span = Math.max(t1 - t0, 3600_000);
    const innerW = width - left - PAD.right;
    const x = (t: number) => left + ((t - t0) / span) * innerW;

    const visible = timed.filter((r) => !hidden.has(r.fate));
    const lanes = SEGMENT_ORDER.filter((s) => timed.some((r) => r.segment === s));
    let top = PAD.top;
    const laneBox: { key: SegmentKey; y: number; h: number }[] = [];
    const dots: Dot[] = [];
    for (const lane of lanes) {
      const members = visible
        .filter((r) => r.segment === lane)
        .sort((a, b) => Date.parse(a.p.proposed_at!) - Date.parse(b.p.proposed_at!));
      // Greedy beeswarm: try the lane's midline, then alternate outward.
      // Past the lane's cap, dots overlap rather than grow the lane forever.
      const step = DOT * 2 + 1.5;
      const cap = LANE_MAX / 2 - DOT;
      const placed: { x: number; dy: number }[] = [];
      for (const r of members) {
        const px = x(Date.parse(r.p.proposed_at!));
        let dy = 0;
        for (let k = 0; k < 40; k++) {
          dy = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * step;
          if (!placed.some((q) => Math.abs(q.x - px) < step && Math.abs(q.dy - dy) < step)) break;
        }
        placed.push({ x: px, dy: Math.max(-cap, Math.min(cap, dy)) });
      }
      const reach = Math.max(0, ...placed.map((q) => Math.abs(q.dy)));
      const h = Math.max(LANE_MIN, reach * 2 + DOT * 2 + 12);
      const mid = top + h / 2;
      members.forEach((r, j) => dots.push({ ...r, x: placed[j].x, y: mid + placed[j].dy, i: dots.length }));
      laneBox.push({ key: lane, y: top, h });
      top += h + (narrow ? 22 : 8);
    }
    return {
      dots,
      laneBox,
      height: top + PAD.bottom,
      x,
      t0,
      t1,
      left,
      narrow,
    };
  }, [width, timed, hidden]);

  if (timed.length === 0) return null;

  const toggle = (f: Fate) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(f)) next.delete(f);
      else next.add(f);
      return next;
    });

  const fmt = (t: number) =>
    new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  return (
    <section className="panel reveal" ref={ref}>
      <div className="panel-head">
        <h2>Every trade, on the clock</h2>
      </div>
      <div className="fate-keys" role="group" aria-label="Show or hide fates">
        {FATE_ORDER.filter((f) => counts.get(f)).map((f) => (
          <button
            key={f}
            className={`fate-key ${hidden.has(f) ? "off" : ""}`}
            aria-pressed={!hidden.has(f)}
            onClick={() => toggle(f)}
            title={FATES[f].help}
          >
            <i className={`fate-glyph fate-${f}`} aria-hidden="true">
              {FATES[f].glyph}
            </i>
            {FATES[f].label}
            <span className="fate-count">{counts.get(f)}</span>
          </button>
        ))}
      </div>
      <div className="chart timeline" ref={wrap}>
        {layout && width !== null && (
          <>
            <svg
              width={width}
              height={layout.height}
              className={`swarm ${seen ? "play" : ""}`}
              role="img"
              aria-label={`${timed.length} proposals from ${fmt(layout.t0)} to ${fmt(layout.t1)}. The proposal log below lists each one.`}
              onPointerLeave={() => setHover(null)}
            >
              {layout.laneBox.map((l) => (
                <g key={l.key}>
                  <rect x={layout.left} y={l.y} width={width - layout.left - PAD.right} height={l.h} rx={8} className="lane" />
                  <text
                    x={layout.narrow ? layout.left + 8 : 0}
                    y={layout.narrow ? l.y - 6 : l.y + l.h / 2 + 4}
                    className="lane-label"
                  >
                    {SEGMENTS[l.key].label}
                  </text>
                </g>
              ))}
              <g className="dots">
                {layout.dots.map((d) => (
                  <DotMark key={d.p.proposal_id} d={d} delay={Math.min(d.i * 5, 1600)} onEnter={() => setHover(d)} />
                ))}
              </g>
              <text x={layout.left} y={layout.height - 6} className="axis-label">
                {fmt(layout.t0)}
              </text>
              <text x={width - PAD.right} y={layout.height - 6} textAnchor="end" className="axis-label">
                {fmt(layout.t1)}
              </text>
            </svg>
            {hover && (
              <div className="tooltip" style={{ left: Math.min(Math.max(hover.x, 110), width - 110), top: hover.y - 10 }}>
                <strong>
                  {hover.p.symbol} {hover.p.side.toUpperCase()}
                </strong>{" "}
                · {isLadder(hover.p) ? ladderLabel(hover.p) : SEGMENTS[hover.segment].label}
                <div className="muted">
                  <span className={`fate-glyph fate-${hover.fate}`}>{FATES[hover.fate].glyph}</span> {FATES[hover.fate].label}
                  {hover.p.notional ? ` · ${money(hover.p.notional)}` : ""}
                </div>
                <div className="muted">{fmt(Date.parse(hover.p.proposed_at!))}</div>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

/** The visible mark is 9px; the transparent disc behind it is the hit target. */
function DotMark({ d, delay, onEnter }: { d: Dot; delay: number; onEnter: () => void }) {
  const open = () => {
    window.location.assign(`/proposals?id=${d.p.proposal_id}`);
  };
  const style = { animationDelay: `${delay}ms` };
  const s = DOT * 1.5;
  return (
    <g onPointerEnter={onEnter} onClick={open} className="dot-g">
      <circle cx={d.x} cy={d.y} r={DOT + 5} className="dot-hit" />
      {d.fate === "awaiting" && <circle cx={d.x} cy={d.y} r={DOT} className="dot-pulse" />}
      {d.fate === "blocked" ? (
        <rect x={d.x - s / 2} y={d.y - s / 2} width={s} height={s} rx={1.5} className={`dot fate-${d.fate}`} style={style} />
      ) : (
        <circle cx={d.x} cy={d.y} r={d.fate === "held" ? DOT - 1 : DOT} className={`dot fate-${d.fate}`} style={style} />
      )}
    </g>
  );
}
