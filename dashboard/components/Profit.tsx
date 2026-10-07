import Link from "next/link";

import { RelTime } from "@/components/Live";
import { CountUp } from "@/components/motion";
import { Badge, CardHead, CoinMark } from "@/components/ui";
import { coinOf, isLadder, ladderLabel, money, price } from "@/lib/format";
import { type PnlSummary, pnlTone, signedMoney } from "@/lib/pnl";

/**
 * The page's headline: what the trades you took have made. Realized is the
 * agent's own figure; open is marked here and says it is an estimate.
 */
export function ProfitHero({ s, accepted }: { s: PnlSummary; accepted: number }) {
  const max = Math.max(1, ...s.bySymbol.map(([, v]) => Math.abs(v)));
  const tone = pnlTone(s.realized);
  return (
    <section className={`card profit ${tone}`} aria-labelledby="profit-title">
      <div className="profit-main">
        <p className="eyebrow" id="profit-title">Realized P&amp;L</p>
        <p className={`profit-value tone-text ${tone}`}>
          <CountUp text={s.realized === null ? "$0.00" : signedMoney(s.realized)} />
        </p>
        <p className="muted small">
          {s.trades.length ? `${s.trades.length} filled` : "No fills yet"}
          {accepted ? ` · ${accepted} accepted` : ""}
        </p>
      </div>

      <dl className="profit-side">
        <div>
          <dt>Open, est.</dt>
          <dd className={`tone-text ${pnlTone(s.open)}`}><CountUp text={signedMoney(s.open)} /></dd>
        </div>
        <div>
          <dt>Today</dt>
          <dd className={`tone-text ${pnlTone(s.today)}`}><CountUp text={signedMoney(s.today)} /></dd>
          {s.tradedToday ? <small>{money(s.tradedToday.toFixed(2))} traded</small> : null}
        </div>
        <div>
          <dt>At work</dt>
          <dd><CountUp text={money(s.atWork.toFixed(2))} /></dd>
        </div>
      </dl>

      {s.bySymbol.length > 0 && (
        <ul className="pnl-bars" aria-label="Realized P&L by coin">
          {s.bySymbol.map(([sym, v]) => (
            <li key={sym}>
              <span className="mono">{coinOf(sym)}</span>
              <span className="pnl-track" aria-hidden="true">
                <span className={`pnl-fill ${v < 0 ? "red" : "mint"}`} style={{ width: `${(Math.abs(v) / max) * 50}%`, [v < 0 ? "right" : "left"]: "50%" }} />
              </span>
              <span className={`mono tone-text ${pnlTone(v)}`}>{signedMoney(v)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Every trade you took, newest first, with what it has made. */
export function TakenLedger({ s }: { s: PnlSummary }) {
  return (
    <section className="card flush">
      <CardHead title="Trades you took" />
      {s.trades.length === 0 ? (
        <p className="muted small pad">None yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="grid-table cards">
            <thead>
              <tr><th>Trade</th><th>Sleeve</th><th className="num">Filled</th><th className="num">In at</th><th className="num">Dollars</th><th className="num" title="Open P&amp;L is estimated at the proposal price">P&amp;L</th></tr>
            </thead>
            <tbody>
              {s.trades.map((t) => (
                <tr key={t.p.proposal_id}>
                  <td className="lead">
                    <Link className="asset" href={`/proposals?id=${t.p.proposal_id}`}>
                      <CoinMark symbol={t.p.symbol} size={28} />
                      <span>
                        <strong>{t.p.side.toUpperCase()} {coinOf(t.p.symbol)}</strong>
                        <small><RelTime iso={t.p.proposed_at} /></small>
                      </span>
                    </Link>
                  </td>
                  <td className="small" data-label="Sleeve">{isLadder(t.p) ? ladderLabel(t.p) : "System 1"}</td>
                  <td className="num mono" data-label="Filled">{t.qty || "—"}</td>
                  <td className="num mono" data-label="In at">{price(t.p.reference_price)}</td>
                  <td className="num mono" data-label="Dollars">{money(t.cost.toFixed(2))}</td>
                  <td className="num" data-label="P&L">
                    {t.move !== null ? (
                      <span className={`mono tone-text ${pnlTone(t.move)}`}>{signedMoney(t.move)}</span>
                    ) : (
                      <Badge>{t.open ? "No mark" : t.p.side === "sell" ? "Exit" : "Closed"}</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** The same headline, small, on every page. */
export function SidebarPnl({ s }: { s: PnlSummary }) {
  return (
    <Link href="/" className="sidebar-pnl">
      <span className="eyebrow">Realized P&amp;L</span>
      <strong className={`tone-text ${pnlTone(s.realized)}`}>{s.realized === null ? "$0.00" : signedMoney(s.realized)}</strong>
      {s.open !== null && <small className={`tone-text ${pnlTone(s.open)}`}>{signedMoney(s.open)} open</small>}
    </Link>
  );
}
