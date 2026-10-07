"use client";

import Link from "next/link";
import { useState } from "react";

import { CsvButton } from "@/components/CsvButton";
import { RelTime } from "@/components/Live";
import { CoinMark, FateBadge, Icon } from "@/components/ui";
import type { Fated } from "@/lib/flow";
import { coinOf, isLadder, ladderLabel, money, verdictOf } from "@/lib/format";

const PAGE_SIZE = 12;
const ANY = "";

export type QueueTab = "awaiting" | "decided" | "all";

/**
 * The working set on the left of the review page. "Needs your call" is the
 * default because most rows in a mature log are history, which would bury
 * the few that wait on you.
 */
export function ProposalQueue({ rows, decidedIds, selectedId, initialTab }: {
  rows: Fated[];
  decidedIds: string[];
  selectedId: string | null;
  initialTab: QueueTab;
}) {
  const decided = new Set(decidedIds);
  const sets: Record<QueueTab, Fated[]> = {
    awaiting: rows.filter((r) => r.fate === "awaiting"),
    decided: rows.filter((r) => decided.has(r.p.proposal_id)),
    all: rows,
  };
  const [tab, setTab] = useState<QueueTab>(initialTab);
  const [query, setQuery] = useState("");
  const [pair, setPair] = useState(ANY);
  const [outcome, setOutcome] = useState(ANY);
  const [page, setPage] = useState(0);

  const q = query.trim().toLowerCase();
  const shown = sets[tab].filter(
    ({ p }) =>
      (!pair || p.symbol === pair) &&
      (!outcome || verdictOf(p) === outcome) &&
      (!q ||
        p.proposal_id.startsWith(q) ||
        p.symbol.toLowerCase().includes(q) ||
        (p.trigger_reason ?? "").toLowerCase().includes(q) ||
        (isLadder(p) && ladderLabel(p).toLowerCase().includes(q)) ||
        (p.risk_failures ?? []).some((r) => r.includes(q))),
  );
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = shown.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
  const pairs = [...new Set(rows.map((r) => r.p.symbol))].sort();
  const reset = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(0);
  };

  const tabs: [QueueTab, string][] = [["awaiting", "Your call"], ["decided", "Decided"], ["all", "All"]];

  return (
    <section className="card flush queue" aria-label="Proposal queue">
      <div className="queue-head">
        <div className="pills" role="tablist" aria-label="Queue">
          {tabs.map(([key, label]) => (
            <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "pill on" : "pill"} onClick={() => reset(setTab)(key)}>
              {label} <span className="pill-count">{sets[key].length}</span>
            </button>
          ))}
        </div>
        <div className="queue-filters">
          <input type="search" placeholder="Search" aria-label="Search proposals" value={query} onChange={(e) => reset(setQuery)(e.target.value)} />
          <select aria-label="Pair" value={pair} onChange={(e) => reset(setPair)(e.target.value)}>
            <option value={ANY}>Any pair</option>
            {pairs.map((s) => <option key={s} value={s}>{coinOf(s)}</option>)}
          </select>
          <select aria-label="Outcome" value={outcome} onChange={(e) => reset(setOutcome)(e.target.value)}>
            <option value={ANY}>Any outcome</option>
            {["win", "loss", "flat", "pending", "unscorable"].map((v) => <option key={v} value={v}>{v[0].toUpperCase() + v.slice(1)}</option>)}
          </select>
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="queue-empty">
          <p>{tab === "awaiting" && !q && !pair && !outcome ? "Queue clear" : "No match"}</p>
          {tab === "awaiting" && rows.length > 0 && (
            <button className="link" onClick={() => reset(setTab)("all")}>Show all {rows.length}</button>
          )}
        </div>
      ) : (
        <ul className="queue-list">
          {visible.map(({ p, fate }) => (
            <li key={p.proposal_id}>
              <Link
                href={`/proposals?id=${p.proposal_id}&tab=${tab}`}
                scroll={false}
                // On a phone the review replaces the queue, so start it at the top.
                onClick={() => window.matchMedia("(max-width: 900px)").matches && window.scrollTo(0, 0)}
                className={p.proposal_id === selectedId ? "queue-item on" : "queue-item"}
                aria-current={p.proposal_id === selectedId ? "true" : undefined}
              >
                <CoinMark symbol={p.symbol} size={28} />
                <span className="queue-copy">
                  <strong>{p.side.toUpperCase()} {coinOf(p.symbol)} · {isLadder(p) ? ladderLabel(p) : "System 1"}</strong>
                  <small className="mono">{p.proposal_id} · <RelTime iso={p.proposed_at} /></small>
                </span>
                <span className="queue-side">
                  <FateBadge fate={fate} />
                  <small className="mono">{money(p.notional)}</small>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}

      <div className="queue-foot">
        {shown.length > PAGE_SIZE ? (
          <nav className="pager" aria-label="Pages">
            <button className="btn small" disabled={current === 0} onClick={() => setPage(current - 1)} aria-label="Newer"><Icon name="arrow-left" size={13} /></button>
            <span className="eyebrow">{current + 1} / {pages}</span>
            <button className="btn small" disabled={current >= pages - 1} onClick={() => setPage(current + 1)} aria-label="Older"><Icon name="arrow-right" size={13} /></button>
          </nav>
        ) : (
          <span className="eyebrow">{shown.length}</span>
        )}
        <span className="hide-sm"><CsvButton proposals={shown.map((r) => r.p)} label="CSV" /></span>
      </div>
    </section>
  );
}
