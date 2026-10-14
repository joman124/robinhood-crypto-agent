import { Badge, CardHead, CheckRow, CoinMark, Fact, Facts } from "@/components/ui";
import { coinOf, money, price } from "@/lib/format";
import { pnlTone, signedMoney } from "@/lib/pnl";
import type { ForwardTest as Test, Trigger, WhatIfRow } from "@/lib/types";

/**
 * The forward test, as `rhca shadow` reports it: the split on paper, the bar
 * it is judged by after 90 closes, how far each coin is from a trade, and what
 * approving every paper trade that passed risk would have made. Paper only —
 * nothing here is a proposal, so nothing here has an Accept button.
 */

const ACCOUNTS = [
  ["split", "Split"],
  ["long-term", "Long-term"],
  ["short-term", "Breakout"],
  ["hold", "Just holding"],
] as const;

const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(1)}%`;
/** How far the price must move from `close` to reach `line`. */
const away = (close: string, line: string) => signed((Number(line) / Number(close) - 1) * 100);

export function ForwardTest({ t }: { t: Test }) {
  const days = t.bar?.days ?? 90;
  const done = t.closes >= days;
  return (
    <>
      <section className="card" aria-labelledby="forward-title">
        <div className="card-top">
          <p className="eyebrow" id="forward-title">Forward test · paper, no orders</p>
          <Badge tone={done ? "mint" : "violet"} icon="calendar">Day {Math.min(t.closes, days)} of {days}</Badge>
        </div>
        <span className="meter" role="img" aria-label={`${t.closes} of ${days} closes`}>
          <span className="meter-fill violet" style={{ width: `${Math.min(100, (t.closes / days) * 100)}%` }} />
        </span>
        <Facts cols={4}>
          {ACCOUNTS.map(([key, label]) => {
            const a = t.accounts[key];
            const move = a ? (Number(a.equity) / Number(a.capital) - 1) * 100 : null;
            return (
              <Fact key={key} label={label}>
                {a ? money(a.equity) : "—"}{" "}
                {move !== null && <small className={`tone-text ${pnlTone(move)}`}>{signed(move)}</small>}
              </Fact>
            );
          })}
        </Facts>

        {t.bar && (
          <>
            <ul className="check-list" aria-label="The bar">
              {t.bar.checks.map((c, i) => {
                const [label, rest] = c.name.split(/:\s(.+)/);
                return (
                  <CheckRow
                    key={c.name}
                    mark={i + 1}
                    label={<><strong>{label}</strong>{rest ? ` — ${rest}` : ""}</>}
                    detail={c.detail}
                    state={c.passed === null ? "pending" : c.passed ? "pass" : "block"}
                  />
                );
              })}
            </ul>
            <p className="muted small">
              {t.bar.conclusion ?? `Every verdict is provisional until the bar is read on ${t.bar.read_on}.`}
            </p>
          </>
        )}

        {t.coverage && t.coverage.missed > 0 && (
          <p className="alert warn compact">
            <strong>{t.coverage.missed} of {t.closes} closes missed</strong> · {t.coverage.missed_days}. rhca run was
            down, so no Robinhood price exists for those days.
          </p>
        )}
      </section>

      <div className="grid-main">
        {t.triggers && <NextTrades rows={t.triggers} through={t.through} />}
        {t.what_if && <WhatIf rows={t.what_if} start={t.start} />}
      </div>
    </>
  );
}

function NextTrades({ rows, through }: { rows: Trigger[]; through: string }) {
  return (
    <section className="card flush">
      <CardHead title="Next paper trades">
        <span className="muted small">as of the {through} close</span>
      </CardHead>
      <div className="table-scroll">
        <table className="grid-table cards">
          <thead>
            <tr><th>Coin</th><th>Long-term buys</th><th>Breakout</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol}>
                <td className="lead">
                  <span className="asset">
                    <CoinMark symbol={r.symbol} size={28} />
                    <span><strong>{coinOf(r.symbol)}</strong><small>closed {price(r.close)}</small></span>
                  </span>
                </td>
                <td className="wide" data-label="Long-term">{longTerm(r)}</td>
                <td className="wide" data-label="Breakout">{breakout(r)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function longTerm(r: Trigger) {
  if (r.tranches === 0) return <span className="muted">—</span>;
  if (r.bought >= r.tranches) return <span className="muted">All {r.tranches} bought</span>;
  const count = <small className="muted"> · {r.bought}/{r.tranches}</small>;
  if (r.buy_under === null) return <span>{r.bought}/{r.tranches} bought</span>;
  const under = Number(r.close) < Number(r.buy_under);
  return (
    <span className="mono">
      under {price(r.buy_under)} <small className={under ? "tone-text mint" : undefined}>{under ? "now" : `(${away(r.close, r.buy_under)})`}</small>{count}
      {r.buy_from && r.buy_from > new Date().toISOString().slice(0, 10) && (
        <small className="muted"> · from {r.buy_from}</small>
      )}
    </span>
  );
}

function breakout(r: Trigger) {
  if (r.stop_under !== null) {
    return <span className="mono">holds · stop {price(r.stop_under)} <small className="tone-text red">({away(r.close, r.stop_under)})</small></span>;
  }
  if (r.enter_over !== null) {
    return <span className="mono">over {price(r.enter_over)} <small>({away(r.close, r.enter_over)})</small></span>;
  }
  return <span className="muted">—</span>;
}

function WhatIf({ rows, start }: { rows: WhatIfRow[]; start: string }) {
  const taken = rows.filter((r) => r.worth !== null);
  const cost = taken.reduce((s, r) => s + Number(r.cost), 0);
  const made = taken.reduce((s, r) => s + Number(r.worth), 0) - cost;
  const paper = taken.reduce((s, r) => s + Number(r.paper_pnl), 0);
  const blocked = rows.filter((r) => r.blocked).length;
  return (
    <section className="card flush">
      <CardHead title="Had you approved them">
        {taken.length > 0 && <strong className={`mono tone-text ${pnlTone(made)}`}>{signedMoney(made)}</strong>}
      </CardHead>
      {rows.length === 0 ? (
        <p className="muted small pad">No paper trades since {start}: neither rule has fired.</p>
      ) : (
        <>
          <p className="muted small pad">
            {taken.length} taken at Robinhood&apos;s prices, {money(cost.toFixed(2))} in; the paper account made{" "}
            {signedMoney(paper)} on the same trades. {blocked} blocked by risk, {rows.length - taken.length - blocked} not checked.
          </p>
          <div className="table-scroll">
            <table className="grid-table cards">
              <thead>
                <tr><th>Trade</th><th>Sleeve</th><th className="num">In</th><th className="num">P&amp;L</th></tr>
              </thead>
              <tbody>
                {[...rows].reverse().map((r) => (
                  <tr key={`${r.day}|${r.sleeve}|${r.symbol}`}>
                    <td className="lead">
                      <span className="asset">
                        <CoinMark symbol={r.symbol} size={28} />
                        <span><strong>Buy {coinOf(r.symbol)}</strong><small>{r.day}{r.sold ? ` → sold ${r.sold}` : ""}</small></span>
                      </span>
                    </td>
                    <td className="small" data-label="Sleeve">{r.sleeve}</td>
                    <td className="num mono" data-label="In">{money(r.cost)}</td>
                    <td className="num" data-label="P&L">
                      {r.worth !== null ? (
                        <span className={`mono tone-text ${pnlTone(Number(r.worth) - Number(r.cost))}`}>
                          {signedMoney(Number(r.worth) - Number(r.cost))}
                        </span>
                      ) : (
                        <Badge tone={r.blocked ? "red" : "neutral"} title={r.note}>{r.blocked ? "Blocked" : "Not checked"}</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
