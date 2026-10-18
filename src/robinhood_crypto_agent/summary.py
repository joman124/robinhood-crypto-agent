"""``rhca report``: the forward test at a glance, from the audit log alone.

No Coinbase fetch and no Robinhood call: this reads only what ``rhca run``
already wrote -- the ``shadow_day`` paper-account records and the proposals --
and prints a verdict-oriented summary, so the owner can read what the split
found over two weeks on paper without grepping raw JSON (proposal p7).

Two neighbours do more, with a price fetch: ``rhca shadow`` replays every close
from Coinbase and renders the full report and the bar; ``rhca accuracy`` scores
whether each proposal's price then moved the proposed way. This command does
neither -- it is the offline glance, and it runs with the network unplugged.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Sequence

from .audit import KIND_PROPOSAL, AuditLog
from .backtest import money, pct, rate
from .config import AgentConfig
from .numeric import ZERO, to_decimal
from .shadow import _shadow, logged_days

#: The ramp an equity curve is drawn with, low to high. ASCII on purpose:
#: the owner's Windows console is cp1252, and a Unicode block sparkline raises
#: UnicodeEncodeError there rather than printing.
RAMP = "_.-=+*#@"


def sparkline(values: Sequence[Decimal]) -> str:
    """One ramp glyph per point, scaled between the series' own low and high.
    A flat series is a flat line: there is nothing to scale it against."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return "-" * len(values)
    span = hi - lo
    last = len(RAMP) - 1
    return "".join(RAMP[int((v - lo) / span * last)] for v in values)


def max_drawdown_pct(values: Sequence[Decimal]) -> Decimal:
    """The deepest peak-to-trough fall in the series, as a percent of the peak."""
    peak = worst = ZERO
    for v in values:
        peak = max(peak, v)
        if peak > ZERO:
            worst = max(worst, (peak - v) / peak * 100)
    return worst


def _event_date(record: dict) -> date | None:
    try:
        return date.fromisoformat(str(record.get("recorded_at", ""))[:10])
    except ValueError:
        return None


def _by_coin(proposals: Sequence[dict]) -> list[tuple[str, int, int]]:
    """Per coin: how many proposals, and how many passed every risk rule."""
    counted: Counter = Counter()
    passed: Counter = Counter()
    for p in proposals:
        coin = p.get("symbol") or "?"
        counted[coin] += 1
        if p.get("risk_passed"):
            passed[coin] += 1
    return [(coin, counted[coin], passed[coin]) for coin in sorted(counted)]


def _equities(logged: dict, days: Sequence[str], account: str) -> list[Decimal]:
    return [
        to_decimal(logged[d]["accounts"][account]["equity"], field="equity") for d in days
    ]


def render(config: AgentConfig, audit: AuditLog) -> str:
    """``rhca report``: the summary, read straight from the log."""
    shadow = _shadow(config)
    start = shadow.start.date()
    lines = [
        "=" * 72,
        "FORWARD TEST SUMMARY  (from the log; no price fetch, no broker)",
        "=" * 72,
        f"Counting from the {start} daily close. `rhca shadow` replays the full "
        "report; `rhca accuracy` scores each proposal.",
    ]

    logged = logged_days(audit, config)
    days = sorted(logged)
    if not days:
        lines += [
            "",
            "  No close recorded yet -- `rhca run` records one per UTC daily close.",
        ]
        return "\n".join(lines)

    split = _equities(logged, days, "split")
    hold = _equities(logged, days, "hold")
    capital = shadow.capital
    fills = sum(len(logged[d].get("fills") or []) for d in days)
    pnl = split[-1] - capital
    ret = (split[-1] / capital - 1) * 100 if capital > ZERO else ZERO
    hold_pnl = hold[-1] - capital
    hold_ret = (hold[-1] / capital - 1) * 100 if capital > ZERO else ZERO

    lines += [
        "",
        f"  closes recorded : {len(days)}  ({days[0]} -> {days[-1]})",
        f"  paper fills     : {fills}"
        + ("  (nothing has triggered a paper trade yet)" if not fills else ""),
        "",
        f"  split equity    : {money(split[-1])}  "
        f"({pct(ret)}, {money(pnl)} on {money(capital)} paper P&L)",
        f"  hold  equity    : {money(hold[-1])}  "
        f"({pct(hold_ret)}, {money(hold_pnl)})  -- buy-and-hold benchmark",
        f"  max drawdown    : {float(max_drawdown_pct(split)):.1f}%  (split equity)",
        "",
        f"  split equity  {sparkline(split)}  {money(split[0])} -> {money(split[-1])}",
        f"  hold  equity  {sparkline(hold)}  {money(hold[0])} -> {money(hold[-1])}",
    ]

    proposals = list(audit.events(kind=KIND_PROPOSAL))
    since = [p for p in proposals if (_event_date(p) or date.min) >= start]
    lines += ["", f"  proposals since {start} : {len(since)}"]
    rows = _by_coin(since)
    if not since and proposals:
        dates = [d for d in (_event_date(p) for p in proposals) if d]
        span = f"{min(dates)} -> {max(dates)}" if dates else "unknown dates"
        lines.append(
            f"  the {len(proposals)} proposal(s) in the log all predate the test "
            f"({span}); summarized below for context."
        )
        rows = _by_coin(proposals)

    if rows:
        lines += [
            "",
            "  pass rate by coin (share that passed all risk rules -- not a win rate;",
            "  for whether the price then moved the proposed way, use `rhca accuracy`):",
        ]
        for coin, n, p in rows:
            lines.append(
                f"    {coin:<9} {p:>4}/{n:<4} passed  ({rate(p / n if n else None)})"
            )
    return "\n".join(lines)
