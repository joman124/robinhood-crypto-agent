/**
 * How far each coin's last daily close sits from a line that would make the
 * split trade: the breakout enters over the 20-day high + 100-day average
 * (`enter_over`), the long-term sleeve buys under the 200-day average
 * (`buy_under`), and a held breakout sells under its trailing stop
 * (`stop_under`). The gap is the signed price move the close must make to
 * reach the line — positive up, negative down — so the nearest is the one
 * needing the smallest move. Pure and import-free, so `node
 * lib/triggers.check.mjs` can run it.
 */

import type { Trigger } from "./types";

export type GapLabel = "breakout" | "split" | "stop";

export interface Gap {
  label: GapLabel;
  /** The trigger price, as sent. */
  line: string;
  /** Signed % the close must move to reach the line: + up, − down. */
  gapPct: number;
}

export interface TriggerGap {
  symbol: string;
  close: string;
  breakout: Gap | null;
  split: Gap | null;
  stop: Gap | null;
  /** The live line needing the smallest absolute move; null when none is set. */
  nearest: Gap | null;
}

const num = (v: string | null | undefined): number | null => {
  const n = Number(v);
  return v != null && v !== "" && Number.isFinite(n) ? n : null;
};

function gap(close: number, line: string | null | undefined, label: GapLabel): Gap | null {
  const l = num(line);
  if (l === null || close <= 0) return null;
  return { label, line: String(line), gapPct: (l / close - 1) * 100 };
}

export function triggerGap(t: Trigger): TriggerGap {
  const close = num(t.close) ?? 0;
  const breakout = gap(close, t.enter_over, "breakout");
  const split = gap(close, t.buy_under, "split");
  const stop = gap(close, t.stop_under, "stop");
  const lines = [breakout, split, stop].filter((g): g is Gap => g !== null);
  const nearest = lines.length
    ? lines.reduce((a, b) => (Math.abs(b.gapPct) < Math.abs(a.gapPct) ? b : a))
    : null;
  return { symbol: t.symbol, close: t.close, breakout, split, stop, nearest };
}
