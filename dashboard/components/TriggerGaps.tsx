import { CoinMark } from "@/components/ui";
import { coinOf, price } from "@/lib/format";
import { type Gap, triggerGap } from "@/lib/triggers";
import type { Trigger } from "@/lib/types";

/**
 * The forward-test status board, below the loop strip: for each watchlist coin,
 * how far its last close sits from a line that would make the split trade. Read
 * only — these are trigger distances, never proposals. The detail (sleeve state,
 * paper fills) lives in the command center's forward-test card.
 */

const gapText = (g: Gap): string => {
  const dir = g.gapPct > 0 ? "↑" : g.gapPct < 0 ? "↓" : "";
  return `${dir}${Math.abs(g.gapPct).toFixed(1)}%`;
};

/** Mint when it is almost firing, amber when it is close, else quiet. */
const gapTone = (g: Gap): string => {
  const d = Math.abs(g.gapPct);
  return d < 2 ? "mint" : d < 8 ? "amber" : "neutral";
};

export function TriggerGaps({ triggers }: { triggers: Trigger[] | null | undefined }) {
  if (!triggers || triggers.length === 0) return null;
  const rows = triggers.map(triggerGap).filter((r) => r.nearest !== null);
  if (rows.length === 0) return null;

  return (
    <section className="trigger-gaps" aria-label="Distance to the next trigger">
      <p className="eyebrow">Distance to trigger</p>
      <ul>
        {rows.map((r) => (
          <li key={r.symbol}>
            <span className="tg-coin">
              <CoinMark symbol={r.symbol} size={16} />
              <strong>{coinOf(r.symbol)}</strong>
            </span>
            <span
              className={`tg-gap mono tone-text ${gapTone(r.nearest!)}`}
              title={`nearest trigger: ${r.nearest!.label} at ${price(r.nearest!.line)}`}
            >
              {gapText(r.nearest!)}
            </span>
            <span className="tg-detail mono">
              <span className="muted">@{price(r.close)}</span>
              {r.breakout && <span title="breakout: 20-day high + 100-day average">↑{price(r.breakout.line)}</span>}
              {r.split && <span title="split: 200-day average">↓{price(r.split.line)}</span>}
              {r.stop && <span className="tone-text red" title="breakout trailing stop">■{price(r.stop.line)}</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
