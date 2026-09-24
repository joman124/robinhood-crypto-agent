"use client";

import { useEffect, useMemo, useState } from "react";

import { DecisionControls } from "@/components/DecisionControls";
import { RelTime } from "@/components/Live";
import { ProposalDetail } from "@/components/ProposalDetail";
import { StatusChip, VerdictChip } from "@/components/Verdict";
import { STATUS, acceptBlocker, money, pct, score, signedPct, statusLabel, toCsv } from "@/lib/format";
import type { Decision, Payload, Proposal } from "@/lib/types";

const PAGE_SIZE = 25;
const HASH = /^#p=([0-9a-f]{6,64})$/;

type Tab = "awaiting" | "decided" | "all";
const ANY = "";

export function ProposalLog({
  payload,
  decisions,
  canDecide,
}: {
  payload: Payload | null;
  decisions: Decision[];
  canDecide: boolean;
}) {
  const proposals = useMemo(() => payload?.proposals ?? [], [payload]);
  const byId = useMemo(() => new Map(decisions.map((d) => [d.proposal_id, d])), [decisions]);

  // "Needs your call" is the working set: actionable and not yet decided. Most
  // rows in a mature log are history, so the whole list would bury them.
  const awaiting = proposals.filter((p) => p.actionable && !byId.has(p.proposal_id));
  const decided = proposals.filter((p) => byId.has(p.proposal_id));

  const [tab, setTab] = useState<Tab>(awaiting.length > 0 ? "awaiting" : "all");
  const [stage, setStage] = useState(ANY);
  const [pair, setPair] = useState(ANY);
  const [verdict, setVerdict] = useState(ANY);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  // Deep link: #p=<proposal id> opens that proposal.
  useEffect(() => {
    const fromHash = () => setOpenId(HASH.exec(window.location.hash)?.[1] ?? null);
    fromHash();
    window.addEventListener("hashchange", fromHash);
    return () => window.removeEventListener("hashchange", fromHash);
  }, []);

  const open = (id: string | null) => {
    setOpenId(id);
    const url = id ? `#p=${id}` : window.location.pathname + window.location.search;
    window.history.replaceState(null, "", url);
  };

  const base = tab === "awaiting" ? awaiting : tab === "decided" ? decided : proposals;
  const q = query.trim().toLowerCase();
  const shown = base.filter(
    (p) =>
      (!stage || (p.status ?? "proposed") === stage) &&
      (!pair || p.symbol === pair) &&
      (!verdict || (p.outcome?.verdict ?? "pending") === verdict) &&
      (!q ||
        p.proposal_id.startsWith(q) ||
        p.symbol.toLowerCase().includes(q) ||
        (p.trigger_reason ?? "").toLowerCase().includes(q) ||
        (p.risk_failures ?? []).some((r) => r.includes(q))),
  );
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const rows = shown.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  const pairs = [...new Set(proposals.map((p) => p.symbol))].sort();
  const stages = Object.keys(STATUS).filter((s) => proposals.some((p) => (p.status ?? "proposed") === s));
  const filtered = Boolean(stage || pair || verdict || q);

  const reset = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(0);
  };

  const download = () => {
    const blob = new Blob([toCsv(shown)], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `rhca-proposals-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const selected = openId ? proposals.find((p) => p.proposal_id === openId) ?? null : null;

  if (proposals.length === 0) {
    return (
      <section className="panel empty">
        <h2>Proposals</h2>
        <p>No proposals yet.</p>
        <p className="muted">
          Run <code>rhca dashboard-sync</code> where the agent lives, or set{" "}
          <code>RHCA_DASHBOARD_URL</code> and <code>RHCA_DASHBOARD_TOKEN</code> so{" "}
          <code>rhca run</code> pushes every cycle.
        </p>
      </section>
    );
  }

  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "awaiting", label: "Needs your call", count: awaiting.length },
    { key: "decided", label: "Decided", count: decided.length },
    { key: "all", label: "All", count: proposals.length },
  ];

  return (
    <section id="proposals">
      <div className="log-head">
        <div className="filters" role="tablist" aria-label="Proposals">
          {tabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={tab === t.key ? "filter on" : "filter"}
              onClick={() => reset(setTab)(t.key)}
            >
              {t.label} <span className="count">{t.count}</span>
            </button>
          ))}
        </div>
        <div className="controls">
          <input
            type="search"
            placeholder="Search id, pair, rule…"
            aria-label="Search proposals"
            value={query}
            onChange={(e) => reset(setQuery)(e.target.value)}
          />
          <select aria-label="Stage" value={stage} onChange={(e) => reset(setStage)(e.target.value)}>
            <option value={ANY}>Any stage</option>
            {stages.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
          <select aria-label="Pair" value={pair} onChange={(e) => reset(setPair)(e.target.value)}>
            <option value={ANY}>Any pair</option>
            {pairs.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <select aria-label="Outcome" value={verdict} onChange={(e) => reset(setVerdict)(e.target.value)}>
            <option value={ANY}>Any outcome</option>
            {["win", "loss", "flat", "pending", "unscorable"].map((v) => (
              <option key={v} value={v}>
                {v[0].toUpperCase() + v.slice(1)}
              </option>
            ))}
          </select>
          <button onClick={download} disabled={shown.length === 0} title="Download the filtered rows as CSV">
            CSV
          </button>
        </div>
      </div>

      <div className="table-wrap">
        {rows.length === 0 ? (
          <div className="empty">
            <p>
              {tab === "awaiting" && !filtered
                ? "Nothing is waiting on you. Every actionable proposal has been decided."
                : "No proposals match these filters."}
            </p>
            {tab === "awaiting" && !filtered && proposals.length > 0 && (
              <button className="link" onClick={() => reset(setTab)("all")}>
                Show all {proposals.length}
              </button>
            )}
          </div>
        ) : (
          <table className="log">
            <thead>
              <tr>
                <th>Proposed</th>
                <th>Pair</th>
                <th className="num">Notional</th>
                <th title="Composite score from −1 (short) to +1 (long), then confidence: how much of the configured signal weight reported.">
                  Signal <span aria-hidden="true">ⓘ</span>
                </th>
                <th>Stage</th>
                <th>Risk</th>
                <th>Outcome</th>
                <th>Your call</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <Row
                  key={p.proposal_id}
                  p={p}
                  payload={payload}
                  decision={byId.get(p.proposal_id)}
                  canDecide={canDecide}
                  onOpen={() => open(p.proposal_id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {shown.length > PAGE_SIZE && (
        <nav className="pager" aria-label="Pages">
          <button disabled={current === 0} onClick={() => setPage(current - 1)}>
            ← Newer
          </button>
          <span className="muted small">
            {current * PAGE_SIZE + 1}–{Math.min((current + 1) * PAGE_SIZE, shown.length)} of {shown.length}
          </span>
          <button disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
            Older →
          </button>
        </nav>
      )}

      <ProposalDetail
        proposal={selected}
        payload={payload}
        decision={selected ? byId.get(selected.proposal_id) : undefined}
        canDecide={canDecide}
        onClose={() => open(null)}
      />
    </section>
  );
}

function Row({
  p,
  payload,
  decision,
  canDecide,
  onOpen,
}: {
  p: Proposal;
  payload: Payload | null;
  decision: Decision | undefined;
  canDecide: boolean;
  onOpen: () => void;
}) {
  const failures = p.risk_failures ?? [];
  return (
    <tr onClick={onOpen} className="clickable">
      <td data-label="Proposed">
        <RelTime iso={p.proposed_at} />
        <div className="pid">{p.proposal_id}</div>
      </td>
      <td data-label="Pair">
        <button
          className="row-open"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          aria-label={`Details for ${p.symbol} ${p.side} ${p.proposal_id}`}
        >
          {p.symbol}
        </button>{" "}
        <span className={`side ${p.side}`}>{p.side === "buy" ? "▲ BUY" : "▼ SELL"}</span>
      </td>
      <td data-label="Notional" className="num">
        {money(p.notional)}
      </td>
      <td data-label="Signal" className="num">
        {score(p.score)}
        <span className="muted"> · {pct(p.confidence)}</span>
      </td>
      <td data-label="Stage">
        <StatusChip status={p.status} />
      </td>
      <td data-label="Risk">
        {p.risk_passed ? (
          <span className="chip ok">
            <span className="glyph" aria-hidden="true">✓</span>
            Passed
          </span>
        ) : (
          <span className="chip blocked" title={failures.join(", ") || "blocked"}>
            <span className="glyph" aria-hidden="true">✗</span>
            {failures.length > 2 ? `${failures.slice(0, 2).join(", ")} +${failures.length - 2}` : failures.join(", ") || "Blocked"}
          </span>
        )}
      </td>
      <td data-label="Outcome">
        <VerdictChip verdict={p.outcome?.verdict ?? "pending"} title={p.outcome?.reason} />
        {p.outcome?.signed_move_pct != null && <div className="pid">{signedPct(p.outcome.signed_move_pct)}</div>}
      </td>
      <td data-label="Your call" onClick={(e) => e.stopPropagation()}>
        <DecisionControls
          proposal={p}
          decision={decision}
          canDecide={canDecide}
          blocker={acceptBlocker(p, payload)}
          syncedAt={payload?.generated_at ?? null}
        />
      </td>
    </tr>
  );
}
