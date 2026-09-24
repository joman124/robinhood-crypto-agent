"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { Proposal } from "@/lib/types";

const H = 180;
const PAD = { top: 14, right: 16, bottom: 26, left: 40 };

type Point = { t: number; rate: number; wins: number; losses: number; symbol: string; verdict: string };

/**
 * Cumulative hit rate as outcomes resolve, oldest first.
 *
 * Only wins and losses move the line, for the same reason the headline hit
 * rate excludes flats. A single series needs no legend; the panel title
 * names it. The dashed 50% line is the coin flip it has to beat.
 */
export function HitRateTrend({ proposals }: { proposals: Proposal[] }) {
  const points = useMemo(() => {
    const decisive = proposals
      .map((p) => p.outcome)
      .filter((o) => o && (o.verdict === "win" || o.verdict === "loss") && o.resolved_at)
      .sort((a, b) => Date.parse(a!.resolved_at!) - Date.parse(b!.resolved_at!));
    let wins = 0;
    let losses = 0;
    return decisive.map((o): Point => {
      if (o!.verdict === "win") wins++;
      else losses++;
      return {
        t: Date.parse(o!.resolved_at!),
        rate: wins / (wins + losses),
        wins,
        losses,
        symbol: o!.symbol,
        verdict: o!.verdict,
      };
    });
  }, [proposals]);

  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  // Re-attach when the chart first becomes drawable: an auto-refresh can bring
  // the second outcome in after mount, when there was no element to observe.
  const drawable = points.length >= 2;
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(280, entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, [drawable]);

  if (!drawable) {
    return (
      <section className="panel">
        <h2>Hit rate over time</h2>
        <p className="muted">
          Needs at least two resolved wins or losses. {points.length === 1 ? "One so far." : ""}
        </p>
      </section>
    );
  }

  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const span = Math.max(t1 - t0, 1);
  const w = width ?? 0;
  const innerW = w - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const x = (t: number) => PAD.left + ((t - t0) / span) * innerW;
  const y = (r: number) => PAD.top + (1 - r) * innerH;
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.rate).toFixed(1)}`).join("");
  const last = points[points.length - 1];
  const active = hover !== null ? points[hover] : null;

  const onMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - box.left;
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      if (Math.abs(x(points[i].t) - px) < Math.abs(x(points[best].t) - px)) best = i;
    }
    setHover(best);
  };

  const fmt = (t: number) =>
    new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Hit rate over time</h2>
        <span className="muted small">
          cumulative · {last.wins}W / {last.losses}L after {points.length} decisive outcomes
        </span>
      </div>
      <div className="chart" ref={wrap}>
        {/* Measured before drawn: a guessed width would overflow a phone for a frame. */}
        {width !== null && (
        <>
        <svg
          width={w}
          height={H}
          role="img"
          aria-label={`Cumulative hit rate, now ${(last.rate * 100).toFixed(0)}% over ${points.length} outcomes`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {[0, 0.5, 1].map((r) => (
            <g key={r}>
              <line
                x1={PAD.left}
                x2={w - PAD.right}
                y1={y(r)}
                y2={y(r)}
                className={r === 0.5 ? "ref-line" : r === 0 ? "baseline" : "gridline"}
              />
              <text x={PAD.left - 8} y={y(r) + 4} textAnchor="end" className="axis-label">
                {r * 100}%
              </text>
            </g>
          ))}
          <text x={PAD.left} y={H - 6} className="axis-label">
            {fmt(t0)}
          </text>
          <text x={w - PAD.right} y={H - 6} textAnchor="end" className="axis-label">
            {fmt(t1)}
          </text>
          <path d={path} className="trend-line" />
          <circle cx={x(last.t)} cy={y(last.rate)} r={4} className="trend-dot" />
          {active && (
            <>
              <line x1={x(active.t)} x2={x(active.t)} y1={PAD.top} y2={H - PAD.bottom} className="crosshair" />
              <circle cx={x(active.t)} cy={y(active.rate)} r={5} className="trend-dot hover" />
            </>
          )}
        </svg>
        {active && (
          <div
            className="tooltip"
            style={{ left: Math.min(Math.max(x(active.t), 90), w - 90), top: y(active.rate) - 8 }}
          >
            <strong>{(active.rate * 100).toFixed(0)}%</strong> hit rate
            <div className="muted">
              {active.wins}W / {active.losses}L · {fmt(active.t)}
            </div>
            <div className="muted">
              this one: {active.symbol} {active.verdict}
            </div>
          </div>
        )}
        </>
        )}
      </div>
    </section>
  );
}
