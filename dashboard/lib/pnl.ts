/**
 * Money from the trades you took: the proposals the agent recorded a fill for.
 *
 * Realized P&L is the agent's own number, from its recorded fills. Open P&L is
 * an estimate made here: filled quantity × (last mark − proposal price). The
 * fill price is not synced, so the proposal's reference price stands in for it.
 * Pure, and free of runtime imports, so `node lib/pnl.check.mjs` can run it.
 */

import type { Payload, Proposal } from "./types";

export interface TakenTrade {
  p: Proposal;
  qty: number;
  /** Dollars put in (or taken out, for a sell), at the proposal price. */
  cost: number;
  /** False for a sell, and for a breakout entry a later recorded stop closed. */
  open: boolean;
  /** Estimated open P&L at the last mark; null when closed or unmarked. */
  move: number | null;
}

const num = (v: string | null | undefined): number | null => {
  const n = Number(v);
  return v != null && v !== "" && Number.isFinite(n) ? n : null;
};

export function takenTrades(payload: Payload | null): TakenTrade[] {
  const proposals = payload?.proposals ?? [];
  const placed = proposals.filter((p) => (p.execution?.tranches ?? 0) > 0);
  return placed.map((p) => {
    const qty = num(p.execution?.filled_quantity) ?? 0;
    const ref = num(p.reference_price);
    const mark = num(payload?.market?.[p.symbol]?.mark);
    // Long-term tranches are never sold. A breakout entry is closed once a
    // later short-term stop on the same coin has a recorded fill.
    // ponytail: matches entry to stop by coin and time, not lot by lot; the agent's split_book is the exact ledger.
    const closed =
      p.side === "sell" ||
      (p.sleeve === "short-term" &&
        placed.some(
          (s) => s.side === "sell" && s.sleeve === "short-term" && s.symbol === p.symbol && (s.proposed_at ?? "") > (p.proposed_at ?? ""),
        ));
    return {
      p,
      qty,
      cost: ref !== null && qty > 0 ? qty * ref : num(p.notional) ?? 0,
      open: !closed,
      move: !closed && mark !== null && ref !== null && qty > 0 ? qty * (mark - ref) : null,
    };
  });
}

export interface PnlSummary {
  /** The agent's realized P&L per symbol: the split's, or the retired ladder's on older payloads. */
  bySymbol: [string, number][];
  realized: number | null;
  open: number | null;
  /** Dollars still at work in open buys, at proposal price. */
  atWork: number;
  today: number | null;
  tradedToday: number | null;
  trades: TakenTrade[];
}

export function pnlSummary(payload: Payload | null): PnlSummary {
  const source = payload?.split_realized_pnl ?? payload?.ladder_realized_pnl ?? {};
  const bySymbol = Object.entries(source)
    .map(([s, v]): [string, number] => [s, num(v) ?? 0])
    .sort((a, b) => b[1] - a[1]);
  const trades = takenTrades(payload);
  const marked = trades.filter((t) => t.move !== null);
  return {
    bySymbol,
    realized: bySymbol.length ? bySymbol.reduce((s, [, v]) => s + v, 0) : null,
    open: marked.length ? marked.reduce((s, t) => s + (t.move ?? 0), 0) : null,
    atWork: trades.filter((t) => t.open).reduce((s, t) => s + t.cost, 0),
    today: num(payload?.today?.realized_pnl),
    tradedToday: num(payload?.today?.executed_notional),
    trades,
  };
}

/** "+$12.40" / "−$3.05" / "—": the sign always shows, because direction is the point. */
export function signedMoney(n: number | null): string {
  if (n === null) return "—";
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}$${abs}`;
}

export function pnlTone(n: number | null): "mint" | "red" | "neutral" {
  return n === null || Math.abs(n) < 0.005 ? "neutral" : n > 0 ? "mint" : "red";
}
