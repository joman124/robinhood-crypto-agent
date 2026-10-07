"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import { download } from "@/components/CsvButton";
import { RelTime } from "@/components/Live";
import { Badge, Icon } from "@/components/ui";
import type { AuditEvent, EventType } from "@/lib/events";

const PAGE_SIZE = 10;
const TABS: [EventType | "all", string][] = [
  ["all", "All"],
  ["proposal", "Proposals"],
  ["risk", "Risk"],
  ["decision", "Decisions"],
  ["control", "Controls"],
];

function utc(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 19);
}

export function AuditLog({ events }: { events: AuditEvent[] }) {
  const [tab, setTab] = useState<EventType | "all">("all");
  const [page, setPage] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(events[0]?.key ?? null);
  const detail = useRef<HTMLElement>(null);
  // On a phone the detail sits below the list, so bring it into view.
  const select = (key: string) => {
    setSelectedKey(key);
    if (window.matchMedia("(max-width: 1180px)").matches) {
      requestAnimationFrame(() => detail.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  };

  const shown = tab === "all" ? events : events.filter((e) => e.type === tab);
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = shown.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
  const selected = events.find((e) => e.key === selectedKey) ?? visible[0];

  const exportJsonl = () =>
    download(
      `rhca-audit-${new Date().toISOString().slice(0, 10)}.jsonl`,
      "application/x-ndjson",
      shown.map(({ key: _key, ...e }) => JSON.stringify(e)).join("\n"),
    );

  return (
    <>
      <div className="tabs-row">
        <div className="pills" role="tablist" aria-label="Event type">
          {TABS.map(([key, label]) => (
            <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "pill on" : "pill"} onClick={() => { setTab(key); setPage(0); }}>
              {label}
            </button>
          ))}
        </div>
        <button className="btn hide-sm" onClick={exportJsonl} disabled={shown.length === 0}>
          <Icon name="download" /> JSONL
        </button>
      </div>

      <div className="grid-audit">
        <section className="card flush">
          <div className="table-scroll">
            <table className="grid-table audit cards">
              <thead>
                <tr><th>UTC time</th><th>Type</th><th>Actor</th><th>Event</th><th>Reference</th><th>Result</th></tr>
              </thead>
              <tbody>
                {visible.map((e) => (
                  <tr
                    key={e.key}
                    className={e.key === selected?.key ? "selected" : undefined}
                    onClick={() => select(e.key)}
                  >
                    <td className="mono small" data-label="UTC">{utc(e.at)}</td>
                    <td className="badge-cell"><Badge tone={e.tone}>{e.type}</Badge></td>
                    <td className="mono small muted hide-sm">{e.actor}</td>
                    <td className="lead">
                      <button className="row-button" onClick={(ev) => { ev.stopPropagation(); select(e.key); }} aria-pressed={e.key === selected?.key}>{e.event}</button>
                    </td>
                    <td className="mono small muted hide-sm">{e.ref.length > 14 ? `${e.ref.slice(0, 12)}…` : e.ref}</td>
                    <td className={`mono small tone-text ${e.tone}`} data-label="Result">{e.result}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="queue-foot">
            <span className="eyebrow">
              {shown.length ? current * PAGE_SIZE + 1 : 0}–{Math.min((current + 1) * PAGE_SIZE, shown.length)} / {shown.length}
            </span>
            <nav className="pager" aria-label="Pages">
              <button className="btn small" disabled={current === 0} onClick={() => setPage(current - 1)} aria-label="Previous"><Icon name="arrow-left" size={13} /></button>
              <span className="eyebrow">{current + 1} / {pages}</span>
              <button className="btn small" disabled={current >= pages - 1} onClick={() => setPage(current + 1)} aria-label="Next"><Icon name="arrow-right" size={13} /></button>
            </nav>
          </div>
        </section>

        {selected && (
          <section className="card event-detail" ref={detail}>
            <div className="card-head">
              <h2>{selected.event}</h2>
              <Badge tone={selected.tone}>{selected.type}</Badge>
            </div>
            <dl className="facts" style={{ "--cols": 2 } as React.CSSProperties}>
              <div className="fact"><dt>UTC</dt><dd className="mono">{utc(selected.at)}</dd></div>
              <div className="fact"><dt>When</dt><dd><RelTime iso={selected.at} /></dd></div>
              <div className="fact"><dt>Actor</dt><dd className="mono">{selected.actor}</dd></div>
              <div className="fact"><dt>Reference</dt><dd className="mono tone-text mint">{selected.ref}</dd></div>
            </dl>
            <div className="payload">
              <pre>{JSON.stringify(selected.data, null, 2)}</pre>
            </div>
            {(selected.type === "proposal" || selected.type === "risk" || selected.type === "decision") && (
              <Link className="btn wide" href={`/proposals?id=${selected.ref}`}>Open proposal <Icon name="arrow-right" size={12} /></Link>
            )}
          </section>
        )}
      </div>
    </>
  );
}
