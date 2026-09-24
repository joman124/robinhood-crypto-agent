import { STATUS, statusLabel } from "@/lib/format";
import type { Verdict } from "@/lib/types";

/** Where a candidate stopped in the pipeline. Neutral ink: a stage is not a verdict. */
export function StatusChip({ status }: { status: string | null | undefined }) {
  const key = status ?? "proposed";
  return (
    <span className={`stage stage-${key}`} title={STATUS[key]?.help}>
      {statusLabel(key)}
    </span>
  );
}

/**
 * Every status carries a glyph and a word as well as a color, so the meaning
 * survives color-vision deficiency, greyscale printing and forced-colors mode.
 */
const FACE: Record<Verdict, { glyph: string; label: string; cls: string }> = {
  win: { glyph: "✓", label: "Win", cls: "win" },
  loss: { glyph: "✗", label: "Loss", cls: "loss" },
  flat: { glyph: "–", label: "Flat", cls: "flat" },
  pending: { glyph: "⋯", label: "Pending", cls: "pending" },
  unscorable: { glyph: "?", label: "Unscorable", cls: "pending" },
};

export function VerdictChip({ verdict, title }: { verdict: Verdict; title?: string }) {
  const face = FACE[verdict] ?? FACE.unscorable;
  return (
    <span className={`chip ${face.cls}`} title={title}>
      <span className="glyph" aria-hidden="true">
        {face.glyph}
      </span>
      {face.label}
    </span>
  );
}
