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

/** The logo's two chevrons, traced from the source artwork: green "<" over white ">". */
const BRAND_LONG = "M39.3 1L2.6 23.4Q0 25 0 28L0 40Q0 43 2.6 44.6L39.3 67Q41 68 41 66L41 52Q41 51 40.1 50.5L16.5 36.1Q13.1 34 16.5 31.9L40.1 17.5Q41 17 41 16L41 2Q41 0 39.3 1Z";
const BRAND_SHORT = "M45.7 99L82.4 76.6Q85 75 85 72L85 60Q85 57 82.4 55.4L45.7 33Q44 32 44 34L44 48Q44 49 44.9 49.5L68.5 63.9Q71.9 66 68.5 68.1L44.9 82.5Q44 83 44 84L44 98Q44 100 45.7 99Z";

/**
 * The logo, on its black tile. `glow` adds the soft edge light of the source
 * artwork; leave it off where the mark is too small for a blur to read.
 * Its gradient ids are fixed, so render at most one per page.
 */
export function BrandMark({ big, glow }: { big?: boolean; glow?: boolean }) {
  return (
    <span className={big ? "brand-mark big" : "brand-mark"} aria-hidden="true">
      <svg viewBox="0 0 85 100">
        <defs>
          <linearGradient id="brand-long" x1="1" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#2cf5c4" />
            <stop offset="1" stopColor="#05a273" />
          </linearGradient>
          <linearGradient id="brand-short" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#f5f6f8" />
            <stop offset="1" stopColor="#c3c6cc" />
          </linearGradient>
          {glow && (
            <filter id="brand-glow" filterUnits="userSpaceOnUse" x="-12" y="-12" width="109" height="124">
              <feGaussianBlur stdDeviation="3.5" />
              <feMerge>
                <feMergeNode />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          )}
        </defs>
        <g filter={glow ? "url(#brand-glow)" : undefined}>
          <path d={BRAND_LONG} fill="url(#brand-long)" />
          <path d={BRAND_SHORT} fill="url(#brand-short)" />
        </g>
      </svg>
    </span>
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
