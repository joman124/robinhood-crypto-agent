"use client";

import { Icon } from "@/components/ui";
import { toCsv } from "@/lib/format";
import type { Proposal } from "@/lib/types";

export function download(name: string, type: string, body: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([body], { type }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** RFC 4180 CSV of the given proposals, for the questions the console does not answer. */
export function CsvButton({ proposals, label = "Export CSV" }: { proposals: Proposal[]; label?: string }) {
  return (
    <button
      className="btn"
      disabled={proposals.length === 0}
      title="Download these proposals as CSV"
      onClick={() => download(`rhca-proposals-${new Date().toISOString().slice(0, 10)}.csv`, "text/csv", toCsv(proposals))}
    >
      <Icon name="download" /> {label}
    </button>
  );
}
