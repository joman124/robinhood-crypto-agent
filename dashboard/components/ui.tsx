/**
 * The console's building blocks. Server-safe: no hooks, no handlers.
 */

import { coinOf, coinTone } from "@/lib/format";
import { FATES, type Fate } from "@/lib/flow";

/** Lucide icons exported from the Figma file, in `public/icons`. */
export type IconName =
  | "arrow-left" | "arrow-right" | "badge-check" | "ban" | "book-open" | "calendar" | "check"
  | "circle-check" | "circle-help" | "clock" | "download" | "eye" | "file-check" | "info"
  | "layout-dashboard" | "link" | "list-filter" | "lock-keyhole" | "lock-keyhole-open"
  | "octagon-x" | "power" | "scroll-text" | "shield-check" | "shield-off" | "sparkles"
  | "split" | "triangle-alert" | "user-check" | "users" | "x";

/**
 * Drawn as a mask over `currentColor`, so an icon takes its surrounding text
 * color rather than the single color it was exported in.
 */
export function Icon({ name, size = 15 }: { name: IconName; size?: number }) {
  return (
    <span
      className="icon"
      aria-hidden="true"
      style={{ "--icon": `url(/icons/${name}.svg)`, width: size, height: size } as React.CSSProperties}
    />
  );
}

export type Tone = "mint" | "amber" | "red" | "sky" | "violet" | "neutral";

export function Badge({ tone = "neutral", icon, children, title }: {
  tone?: Tone; icon?: IconName; children: React.ReactNode; title?: string;
}) {
  return (
    <span className={`badge ${tone}`} title={title}>
      {icon && <Icon name={icon} size={12} />}
      {children}
    </span>
  );
}

const FATE_TONE: Record<Fate, Tone> = {
  placed: "mint",
  accepted: "mint",
  awaiting: "amber",
  declined: "neutral",
  held: "neutral",
  blocked: "red",
};

/** Glyph and word ride with the color, so a fate never rests on hue alone. */
export function FateBadge({ fate }: { fate: Fate }) {
  return (
    <Badge tone={FATE_TONE[fate]} title={FATES[fate].help}>
      <span aria-hidden="true">{FATES[fate].glyph}</span> {FATES[fate].short}
    </Badge>
  );
}

export function PageHead({ title, children }: { title: React.ReactNode; children?: React.ReactNode }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      {children && <div className="page-actions">{children}</div>}
    </header>
  );
}

export function CardHead({ title, children }: { title: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="card-head">
      <h2>{title}</h2>
      {children}
    </div>
  );
}

export function Facts({ children, cols }: { children: React.ReactNode; cols?: number }) {
  return (
    <dl className="facts" style={cols ? ({ "--cols": cols } as React.CSSProperties) : undefined}>
      {children}
    </dl>
  );
}

export function Fact({ label, children, tone }: { label: string; children: React.ReactNode; tone?: Tone }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd className={tone ? `tone-text ${tone}` : undefined}>{children}</dd>
    </div>
  );
}

export function CoinMark({ symbol, size = 34 }: { symbol: string; size?: number }) {
  const coin = coinOf(symbol);
  return (
    <span className={`coin ${coinTone(symbol)}`} style={{ width: size, height: size }} aria-hidden="true">
      {coin[0]}
    </span>
  );
}

/** A numbered or ticked line with a pass/block badge: risk rules and guarantees. */
export function CheckRow({ mark, label, detail, state, tone }: {
  mark: React.ReactNode; label: React.ReactNode; detail?: React.ReactNode;
  state: "pass" | "block" | "warn"; tone?: "flat";
}) {
  const badge = { pass: ["mint", "Pass"], block: ["red", "Block"], warn: ["amber", "Warn"] } as const;
  return (
    <li className={`check-row ${state} ${tone ?? ""}`}>
      <span className="check-mark">{mark}</span>
      <span className="check-copy">
        <span className="check-label">{label}</span>
        {detail && <span className="check-detail">{detail}</span>}
      </span>
      <Badge tone={badge[state][0]}>{badge[state][1]}</Badge>
    </li>
  );
}
