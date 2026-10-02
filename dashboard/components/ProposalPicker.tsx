"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { coinOf, isLadder, ladderLabel } from "@/lib/format";
import type { Proposal } from "@/lib/types";

const LIMIT = 60;

/** Pick which proposal's evaluation to read; keeps the other query params. */
export function ProposalPicker({ proposals, selected }: { proposals: Proposal[]; selected?: string }) {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const shown = proposals.slice(0, LIMIT);
  if (selected && !shown.some((p) => p.proposal_id === selected)) {
    const extra = proposals.find((p) => p.proposal_id === selected);
    if (extra) shown.push(extra);
  }
  return (
    <label className="picker">
      <span className="sr-only">Proposal</span>
      <select
        value={selected}
        onChange={(e) => {
          const next = new URLSearchParams(params);
          next.set("id", e.target.value);
          router.push(`${path}?${next}`);
        }}
      >
        {shown.map((p) => (
          <option key={p.proposal_id} value={p.proposal_id}>
            {p.risk_passed === false ? "■ " : "● "}
            {coinOf(p.symbol)} {p.side} · {isLadder(p) ? ladderLabel(p) : "System 1"} · {p.proposal_id.slice(0, 8)}
          </option>
        ))}
      </select>
    </label>
  );
}
