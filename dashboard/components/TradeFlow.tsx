"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { useInView } from "@/components/motion";
import { FLOW_NODES, FLOW_ORDER, type FlowNode, type Fated, flowLinks } from "@/lib/flow";

const H = 340;
const PAD = { top: 16, bottom: 16, left: 4 };
const NODE_W = 10;
/** Room for a node's two-line label even when the node itself is a sliver. */
const NODE_GAP = 34;
const LABEL_ROOM = 132;
/** Below this the columns crowd their labels, so the chart scrolls inside its panel instead. */
const MIN_W = 600;

type Laid = { id: FlowNode; count: number; x: number; y: number; h: number };
/** `ribbon` is the filled band; `d` is its centerline, which the particles ride. */
type Band = { key: string; from: FlowNode; to: FlowNode; count: number; d: string; ribbon: string; w: number; tone: string };

/**
 * Where every proposal went: proposed → risk verdict → your call → an order.
 *
 * A Sankey is the form because each proposal takes exactly one route, so the
 * widths at every column add back up to the whole. Particles ride each band
 * at a count proportional to its size, so the busiest route visibly carries
 * the most traffic. Hover a band or node for its share.
 */
export function TradeFlow({ rows }: { rows: Fated[] }) {
  const { nodes, links } = useMemo(() => flowLinks(rows), [rows]);
  const wrap = useRef<HTMLDivElement>(null);
  const [inView, seen] = useInView<HTMLElement>();
  const [width, setWidth] = useState<number | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(MIN_W, entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const total = nodes.get("all") ?? 0;

  const layout = useMemo(() => {
    if (width === null || total === 0) return null;
    const cols = Math.max(...[...nodes.keys()].map((n) => FLOW_NODES[n].col)) + 1;
    const step = (width - PAD.left - LABEL_ROOM - NODE_W) / Math.max(cols - 1, 1);
    const inner = H - PAD.top - PAD.bottom;
    const byCol: FlowNode[][] = Array.from({ length: cols }, () => []);
    FLOW_ORDER.forEach((n) => nodes.has(n) && byCol[FLOW_NODES[n].col].push(n));
    const k = Math.min(...byCol.map((c) => (inner - NODE_GAP * (c.length - 1)) / c.reduce((s, n) => s + nodes.get(n)!, 0)));

    const laid = new Map<FlowNode, Laid>();
    byCol.forEach((col, ci) => {
      const heights = col.map((n) => Math.max(3, nodes.get(n)! * k));
      const used = heights.reduce((a, b) => a + b, 0) + NODE_GAP * (col.length - 1);
      let y = PAD.top + (inner - used) / 2;
      col.forEach((n, i) => {
        laid.set(n, { id: n, count: nodes.get(n)!, x: PAD.left + ci * step, y, h: heights[i] });
        y += heights[i] + NODE_GAP;
      });
    });

    const outAt = new Map<FlowNode, number>();
    const inAt = new Map<FlowNode, number>();
    const bands: Band[] = [];
    const ordered = [...links.entries()].sort(([a], [b]) => {
      const [af, at] = a.split(">") as FlowNode[];
      const [bf, bt] = b.split(">") as FlowNode[];
      return FLOW_ORDER.indexOf(af) - FLOW_ORDER.indexOf(bf) || FLOW_ORDER.indexOf(at) - FLOW_ORDER.indexOf(bt);
    });
    for (const [key, count] of ordered) {
      const [from, to] = key.split(">") as FlowNode[];
      const a = laid.get(from)!;
      const b = laid.get(to)!;
      const ws = Math.max(1.5, (count / a.count) * a.h);
      const wt = Math.max(1.5, (count / b.count) * b.h);
      const s0 = a.y + (outAt.get(from) ?? 0);
      const t0 = b.y + (inAt.get(to) ?? 0);
      outAt.set(from, (outAt.get(from) ?? 0) + ws);
      inAt.set(to, (inAt.get(to) ?? 0) + wt);
      const x0 = a.x + NODE_W;
      const x1 = b.x;
      const mx = (x0 + x1) / 2;
      const curve = (ya: number, yb: number) => `${x0},${ya}C${mx},${ya} ${mx},${yb} ${x1},${yb}`;
      const back = `${x1},${t0 + wt}C${mx},${t0 + wt} ${mx},${s0 + ws} ${x0},${s0 + ws}`;
      bands.push({
        key,
        from,
        to,
        count,
        w: Math.min(ws, wt),
        tone: FLOW_NODES[to].tone,
        d: `M${curve(s0 + ws / 2, t0 + wt / 2)}`,
        ribbon: `M${curve(s0, t0)}L${back}Z`,
      });
    }
    return { laid: [...laid.values()], bands, step, compact: step < 150 };
  }, [width, nodes, links, total]);

  if (total === 0) return null;

  const share = (n: number, of: number) => `${Math.round((n / Math.max(of, 1)) * 100)}%`;
  const focus = hover ? new Set(hover.includes(">") ? hover.split(">") : [hover]) : null;
  const active = (band: Band) => !hover || hover === band.key || hover === band.from || hover === band.to;

  const tip = (() => {
    if (!hover || !layout) return null;
    if (hover.includes(">")) {
      const band = layout.bands.find((b) => b.key === hover);
      if (!band) return null;
      const src = nodes.get(band.from)!;
      return {
        title: `${FLOW_NODES[band.from].label} → ${FLOW_NODES[band.to].label}`,
        body: `${band.count} proposal${band.count === 1 ? "" : "s"} · ${share(band.count, src)} of ${FLOW_NODES[band.from].label.toLowerCase()}`,
      };
    }
    const n = hover as FlowNode;
    return { title: FLOW_NODES[n].label, body: `${nodes.get(n)} · ${share(nodes.get(n)!, total)} of every proposal` };
  })();

  return (
    <section className="panel flow-panel reveal" ref={inView}>
      <div className="panel-head">
        <h2>Where every trade went</h2>
        {tip && <span className="muted small"><strong className="tip-inline">{tip.title}</strong> · {tip.body}</span>}
      </div>
      <div className="flow-chart" ref={wrap}>
        {layout && width !== null && (
          <svg
            width={width}
            height={H}
            className={`flow ${seen ? "play" : ""} ${hover ? "has-focus" : ""}`}
            role="img"
            aria-label={`Of ${total} proposals: ${FLOW_ORDER.filter((n) => n !== "all" && nodes.has(n))
              .map((n) => `${nodes.get(n)} ${FLOW_NODES[n].label.toLowerCase()}`)
              .join(", ")}.`}
            onPointerLeave={() => setHover(null)}
          >
            <g className="bands">
              {layout.bands.map((b, i) => (
                <path
                  key={b.key}
                  d={b.ribbon}
                  className={`band tone-${b.tone} ${active(b) ? "" : "dim"}`}
                  style={{ animationDelay: `${FLOW_NODES[b.from].col * 260 + i * 40}ms` }}
                  onPointerEnter={() => setHover(b.key)}
                />
              ))}
            </g>
            <g className="particles" aria-hidden="true">
              {layout.bands.map((b) => {
                const n = Math.min(16, Math.max(2, Math.round(Math.sqrt(b.count) * 1.6)));
                const dur = 2.6 + (b.key.length % 5) * 0.25;
                return Array.from({ length: n }, (_, i) => {
                  const lane = ((i * 7919) % 97) / 97 - 0.5;
                  return (
                    <g key={`${b.key}-${i}`} transform={`translate(0 ${(lane * b.w * 0.7).toFixed(1)})`}>
                      <circle r={b.w > 6 ? 2.2 : 1.6} className={`particle tone-${b.tone} ${active(b) ? "" : "dim"}`}>
                        <animateMotion dur={`${dur}s`} begin={`${(-i * dur) / n}s`} repeatCount="indefinite" path={b.d} />
                      </circle>
                    </g>
                  );
                });
              })}
            </g>
            <g className="nodes">
              {layout.laid.map((n) => (
                <g
                  key={n.id}
                  className={`node tone-${FLOW_NODES[n.id].tone} ${focus && !focus.has(n.id) ? "dim" : ""}`}
                  onPointerEnter={() => setHover(n.id)}
                  style={{ animationDelay: `${FLOW_NODES[n.id].col * 260}ms` }}
                >
                  <rect x={n.x} y={n.y} width={NODE_W} height={n.h} rx={3} />
                  <text x={n.x + NODE_W + 8} y={n.y + n.h / 2 - 2} className="node-count">
                    {n.count}
                  </text>
                  <text x={n.x + NODE_W + 8} y={n.y + n.h / 2 + 13} className="node-label">
                    {layout.compact ? FLOW_NODES[n.id].short : FLOW_NODES[n.id].label}
                    {n.id !== "all" ? ` · ${share(n.count, total)}` : ""}
                  </text>
                </g>
              ))}
            </g>
          </svg>
        )}
      </div>
      <table className="sr-only">
        <caption>Proposal routes</caption>
        <tbody>
          {[...links.entries()].map(([key, count]) => {
            const [from, to] = key.split(">") as FlowNode[];
            return (
              <tr key={key}>
                <th scope="row">
                  {FLOW_NODES[from].label} to {FLOW_NODES[to].label}
                </th>
                <td>{count}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
