import { CheckRow } from "@/components/ui";
import { RULES, ruleLabel } from "@/lib/format";
import type { Proposal } from "@/lib/types";

/**
 * Every rule's verdict, numbered, pass or block. A rule whose message would
 * quote the account arrives without text, so its generic description stands in.
 * Older payloads carry only the failures' names.
 */
export function RiskChecks({ p, onlyFailed = false }: { p: Proposal; onlyFailed?: boolean }) {
  const findings = p.risk_findings ?? [];
  if (findings.length === 0) {
    const failures = p.risk_failures ?? [];
    if (failures.length === 0) {
      return <p className="muted small">{p.risk_passed ? "All passed" : "—"}</p>;
    }
    return (
      <ul className="check-grid">
        {failures.map((rule, i) => (
          <CheckRow key={rule} mark={String(i + 1).padStart(2, "0")} label={ruleLabel(rule)} detail={RULES[rule]} state="block" />
        ))}
      </ul>
    );
  }
  const numbered = findings.map((f, i) => ({ f, n: String(i + 1).padStart(2, "0") }));
  const shown = onlyFailed ? numbered.filter(({ f }) => !f.passed) : numbered;
  if (shown.length === 0) return <p className="muted small">None</p>;
  return (
    <ul className="check-grid two">
      {shown.map(({ f, n }) => (
        <CheckRow
          key={f.rule}
          mark={n}
          label={RULES[f.rule] ?? ruleLabel(f.rule)}
          detail={f.message ?? (RULES[f.rule] ? ruleLabel(f.rule) : undefined)}
          state={f.passed ? "pass" : f.blocking ? "block" : "warn"}
        />
      ))}
    </ul>
  );
}
