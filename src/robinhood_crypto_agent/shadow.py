"""The forward test: the split account on paper, one UTC daily close at a time.

``rhca backtest --strategies split`` asks how the split *would have* done.
This asks how it *does*: the same two rules, from a start date fixed before
any price it trades existed (``config/shadow.yaml``), on Coinbase's public
daily closes -- one request per coin per day.

After each UTC daily close, ``rhca run`` replays the paper account from the
start date and writes one ``shadow_day`` record to the audit log: what the
paper account did on that close, and Robinhood's bid and ask for every coin it
traded, read at that moment. ``rhca shadow`` replays it again and reports,
against the bar in docs/strategy.md ("The forward test").

Replaying from the start each day, rather than carrying state forward, keeps
the paper account identical to what the backtest code computes for the same
days: there is one implementation of each rule, and no state to drift.

**Nothing here trades.** A shadow day is not a proposal: it carries no
proposal id, ``rhca approve`` cannot find it, and no code path leads from it
to an order. The paper account exists to learn, before any money is at stake,
whether the split behaves live as it did in its backtest, at Robinhood's real
spread.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from .audit import KIND_SHADOW_DAY, AuditLog
from .backtest import DEFAULT_ROUND_TRIP_PCT, money, pct
from .bootstrap import fetch_coinbase_history
from .config import AgentConfig, ShadowConfig
from .errors import AgentError
from .models import Candle, Quote, parse_timestamp, utcnow
from .numeric import ZERO, format_decimal, round_money, to_decimal
from .portfolio_backtest import (
    PortfolioResult,
    Prepared,
    SplitResult,
    run_equal_hold,
    run_split,
)
from .strategy.breakout import Breakout, day_views
from .strategy.hodl import Accumulate

log = logging.getLogger("rhca.shadow")

DAY = timedelta(days=1)
DAILY_MINUTES = 24 * 60
LONG_TERM = "long-term"
SHORT_TERM = "short-term"

#: How often ``rhca run`` looks for a new daily close. A look that finds none
#: costs nothing: the fetch happens once per closed day.
CHECK_INTERVAL_SECONDS = 600
#: How long after a close to wait for a coin Coinbase has not published yet,
#: before recording the day without it.
LATE_GRACE = timedelta(hours=6)

#: The bar, fixed 2026-09-28 before any forward close existed (docs/strategy.md).
BAR_DAYS = 90
#: The breakout's worst 90-day window in the 4-year backtest: its return, and
#: its max drawdown, in percent.
WORST_WINDOW_RETURN_PCT = Decimal("-5.7")
WORST_WINDOW_DRAWDOWN_PCT = Decimal("10.1")


def rules(config: AgentConfig) -> tuple[Breakout, Accumulate]:
    """The two rules, at the sizes ``rhca backtest`` uses by default."""
    shadow = _shadow(config)
    return (
        Breakout(max_weight_pct=config.risk.max_position_pct_of_portfolio),
        Accumulate(mode=shadow.long_mode),
    )


def _shadow(config: AgentConfig) -> ShadowConfig:
    if config.shadow is None:
        raise AgentError(
            "no forward test is configured: config/shadow.yaml is missing "
            "(docs/strategy.md, 'The forward test')"
        )
    return config.shadow


def setup(config: AgentConfig) -> dict[str, Any]:
    """What the test is, stamped on every record: records made under another
    setup belong to another test, and are left out of this one."""
    shadow = _shadow(config)
    return {
        "start": shadow.start.date().isoformat(),
        "capital": str(shadow.capital),
        "long_pct": str(shadow.long_pct),
        "long_mode": shadow.long_mode,
        "symbols": list(shadow.symbols),
        "long_symbols": list(shadow.long_symbols) if shadow.long_symbols else None,
    }


def latest_close(now: datetime) -> datetime:
    """The start of the last UTC day that has closed."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - DAY


def history_days(config: AgentConfig, *, now: datetime) -> int:
    """Daily bars to fetch: every day since the start, and the warm-up the
    slowest average needs before it."""
    shadow = _shadow(config)
    rule, plan = rules(config)
    since = max(0, (now - shadow.start).days)
    return since + max(rule.warmup_days, plan.average_days) + 3


Fetch = Callable[..., list[Candle]]


def fetch_daily(
    symbols: Iterable[str], *, days: int, fetch: Fetch | None = None
) -> dict[str, list[Candle]]:
    """Closed UTC daily bars per coin, oldest first (Coinbase's, by default)."""
    fetch = fetch or fetch_coinbase_history
    return {s: fetch(s, interval_minutes=DAILY_MINUTES, days=days) for s in symbols}


@dataclass(frozen=True)
class PaperFill:
    """One thing the paper account did on one daily close."""

    day: datetime
    sleeve: str
    symbol: str
    side: str
    #: Dollars paid (a buy) or received (a sell), spread included.
    dollars: Decimal
    quantity: Decimal
    #: The daily close it filled against, before the spread.
    close: Decimal

    @property
    def key(self) -> str:
        return f"{self.sleeve}|{self.symbol}|{self.side}"


@dataclass
class Replay:
    """The paper account from the start date through ``through``."""

    through: datetime
    split: SplitResult
    hold: PortfolioResult
    prepared: dict[str, Prepared]
    rule: Breakout
    fills: list[PaperFill]

    @property
    def closes(self) -> int:
        """Daily closes counted so far."""
        return len(self.split.combined.equity_curve)

    def fills_on(self, day: datetime) -> list[PaperFill]:
        return [f for f in self.fills if f.day == day]


def replay(
    daily: Mapping[str, Sequence[Candle]], config: AgentConfig, *, through: datetime
) -> Replay:
    """The split, and hold for comparison, from the start through ``through``."""
    shadow = _shadow(config)
    rule, plan = rules(config)
    prepared = {s: Prepared(list(bars), day_views(bars, rule)) for s, bars in daily.items() if bars}
    window = dict(start=shadow.start, end=through + DAY)
    split = run_split(
        prepared,
        rule,
        plan,
        capital=shadow.capital,
        long_pct=shadow.long_pct,
        round_trip_pct=DEFAULT_ROUND_TRIP_PCT,
        min_trade=config.risk.min_notional_per_trade_usd,
        long_symbols=shadow.long_symbols,
        **window,
    )
    hold = run_equal_hold(
        prepared, capital=shadow.capital, round_trip_pct=DEFAULT_ROUND_TRIP_PCT, **window
    )
    closes = {s: {bar.start: bar.close for bar in p.daily} for s, p in prepared.items()}
    return Replay(through, split, hold, prepared, rule, paper_fills(split, closes))


def paper_fills(
    split: SplitResult, closes: Mapping[str, Mapping[datetime, Decimal]]
) -> list[PaperFill]:
    fills: list[PaperFill] = []
    for trade in split.short.trades:
        fills.append(
            PaperFill(trade.entry_day, SHORT_TERM, trade.symbol, "buy", trade.cost,
                      trade.quantity, closes[trade.symbol][trade.entry_day])
        )
        if trade.exit_day is not None:
            fills.append(
                PaperFill(trade.exit_day, SHORT_TERM, trade.symbol, "sell", trade.proceeds,
                          trade.quantity, closes[trade.symbol][trade.exit_day])
            )
    for trade in split.long.trades:
        fills.append(
            PaperFill(trade.entry_day, LONG_TERM, trade.symbol, "buy", trade.cost,
                      trade.quantity, closes[trade.symbol][trade.entry_day])
        )
    return sorted(fills, key=lambda f: (f.day, f.sleeve, f.symbol, f.side))


def spread_pct(quote: Quote) -> Decimal | None:
    mid = (quote.bid + quote.ask) / 2
    return (quote.ask - quote.bid) / mid * 100 if mid > ZERO else None


def _account(result: PortfolioResult) -> dict[str, str]:
    return {
        "capital": str(round_money(result.capital)),
        "equity": str(round_money(result.final_equity)),
    }


def day_record(
    result: Replay, day: datetime, quotes: Mapping[str, Quote], config: AgentConfig
) -> dict[str, Any]:
    """The ``shadow_day`` audit record for one close."""
    shadow_fills = []
    for fill in result.fills_on(day):
        quote = quotes.get(fill.symbol)
        spread = spread_pct(quote) if quote is not None else None
        shadow_fills.append(
            {
                "sleeve": fill.sleeve,
                "symbol": fill.symbol,
                "side": fill.side,
                "dollars": str(round_money(fill.dollars)),
                "quantity": format_decimal(fill.quantity),
                "close": format_decimal(fill.close),
                "bid": format_decimal(quote.bid) if quote else None,
                "ask": format_decimal(quote.ask) if quote else None,
                "spread_pct": format_decimal(spread.quantize(Decimal("0.0001"))) if spread else None,
            }
        )
    split = result.split
    return {
        "day": day.date().isoformat(),
        "mode": "paper",
        "setup": setup(config),
        "fills": shadow_fills,
        "accounts": {
            LONG_TERM: _account(split.long),
            SHORT_TERM: _account(split.short),
            "split": _account(split.combined),
            "hold": _account(result.hold),
        },
    }


def snapshot(result: Replay, config: AgentConfig) -> dict[str, Any]:
    """What ``rhca status`` shows, written after each recorded day."""
    shadow = _shadow(config)
    split = result.split
    return {
        "updated_at": utcnow().isoformat(),
        "setup": setup(config),
        "start": shadow.start.date().isoformat(),
        "through": result.through.date().isoformat(),
        "closes": result.closes,
        "accounts": {
            LONG_TERM: _account(split.long),
            SHORT_TERM: _account(split.short),
            "split": _account(split.combined),
            "hold": _account(result.hold),
        },
        "open": sorted({f"{t.symbol}" for t in split.short.trades if t.open}),
    }


class ShadowTracker:
    """What ``rhca run`` calls on its forward-test cadence."""

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLog,
        robinhood: Any,
        *,
        fetch: Fetch | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        _shadow(config)
        self.config = config
        self.audit = audit
        self.robinhood = robinhood
        self._fetch = fetch
        self._now = now
        self._recorded = set(logged_days(audit, config))

    def update(self) -> Replay | None:
        """Record the latest closed day, once. ``None`` when there is nothing new."""
        shadow = _shadow(self.config)
        now = self._now()
        through = latest_close(now)
        if through < shadow.start or through.date().isoformat() in self._recorded:
            return None
        daily = fetch_daily(
            shadow.symbols, days=history_days(self.config, now=now), fetch=self._fetch
        )
        late = sorted(s for s, bars in daily.items() if not bars or bars[-1].start < through)
        if late and now < through + DAY + LATE_GRACE:
            raise AgentError(
                f"Coinbase has not published the {through:%Y-%m-%d} daily close for "
                f"{', '.join(late)} yet; trying again shortly"
            )
        result = replay(daily, self.config, through=through)
        today = result.fills_on(through)
        record = day_record(result, through, self._quotes({f.symbol for f in today}), self.config)
        if late:
            record["missing"] = late
        self.audit.append(KIND_SHADOW_DAY, record)
        self._recorded.add(record["day"])
        return result

    def _quotes(self, symbols: set[str]) -> dict[str, Quote]:
        """Robinhood's bid and ask now, for the coins traded on paper today.
        Only a record of the real spread: a failure costs the spread, not the day."""
        if not symbols:
            return {}
        try:
            quotes = self.robinhood.best_bid_ask(sorted(symbols))
        except AgentError as exc:
            log.warning("forward test: could not read Robinhood quotes: %s", exc)
            return {}
        return {q.symbol: q for q in quotes if q.symbol in symbols}


# -- the report --------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    #: ``None`` while there is not yet anything to judge.
    passed: bool | None
    detail: str


def logged_days(audit: AuditLog, config: AgentConfig) -> dict[str, dict[str, Any]]:
    """This test's ``shadow_day`` records, by day (the last wins)."""
    current = setup(config)
    return {
        str(r.get("day")): r
        for r in audit.events(kind=KIND_SHADOW_DAY)
        if r.get("setup") == current
    }


def check_bar(
    result: Replay, window: Replay, logged: Mapping[str, Mapping[str, Any]]
) -> list[Check]:
    """The forward test's three checks. ``window`` is the replay cut at the
    bar's last day, or the whole replay until then."""
    checks: list[Check] = []

    mismatched = []
    compared = 0
    for day_text, record in sorted(logged.items()):
        day = datetime.fromisoformat(day_text).replace(tzinfo=timezone.utc)
        if day > result.through:
            continue
        compared += 1
        logged_keys = sorted(
            f"{f.get('sleeve')}|{f.get('symbol')}|{f.get('side')}" for f in record.get("fills") or []
        )
        if logged_keys != sorted(f.key for f in result.fills_on(day)):
            mismatched.append(day_text)
    checks.append(
        Check(
            "Faithful: every day rhca run logged matches this replay",
            None if compared == 0 else not mismatched,
            (
                "no day logged yet"
                if compared == 0
                else f"{compared - len(mismatched)} of {compared} logged days match"
                + (f"; differs on {', '.join(mismatched)}" if mismatched else "")
            ),
        )
    )

    spreads = [
        to_decimal(f["spread_pct"], field="spread_pct")
        for record in logged.values()
        for f in record.get("fills") or []
        if f.get("spread_pct") is not None
    ]
    median = statistics.median(spreads) if spreads else None
    checks.append(
        Check(
            f"Costs: median Robinhood spread at fill time <= {DEFAULT_ROUND_TRIP_PCT}%",
            None if median is None else median <= DEFAULT_ROUND_TRIP_PCT,
            (
                "no paper fill with a Robinhood quote yet"
                if median is None
                else f"{median:.2f}% over {len(spreads)} fill(s)"
            ),
        )
    )

    short = window.split.short
    in_range = (
        short.total_return_pct >= WORST_WINDOW_RETURN_PCT
        and short.max_drawdown_pct <= WORST_WINDOW_DRAWDOWN_PCT
    )
    checks.append(
        Check(
            f"In range: the breakout sleeve returns >= {WORST_WINDOW_RETURN_PCT}% with a max "
            f"drawdown <= {WORST_WINDOW_DRAWDOWN_PCT}%",
            in_range,
            f"{pct(short.total_return_pct)}, drawdown {pct(-short.max_drawdown_pct)}",
        )
    )
    return checks


def _row(name: str, result: PortfolioResult) -> str:
    return (
        f"  {name:<30}{money(result.capital):>10}{money(result.final_equity):>11}"
        f"{pct(result.total_return_pct):>9}{pct(-result.max_drawdown_pct):>9}"
        f"{float(result.final_exposure_pct):>9.0f}%"
    )


def render(
    result: Replay, window: Replay, config: AgentConfig, logged: Mapping[str, Mapping[str, Any]]
) -> str:
    shadow = _shadow(config)
    split = result.split
    bar_day = shadow.start + DAY * (BAR_DAYS - 1)
    lines = [
        "=" * 110,
        "FORWARD TEST: THE SPLIT ON PAPER",
        "=" * 110,
        "Paper only: nothing here is a proposal or an order. docs/strategy.md, 'The forward test'.",
        f"Counting from the {shadow.start:%Y-%m-%d} daily close: {result.closes} close(s) so far, "
        f"through {result.through:%Y-%m-%d}. The bar is read after {BAR_DAYS} closes, on "
        f"{bar_day + DAY:%Y-%m-%d}.",
        f"{money(split.long.capital)} long-term ({Accumulate(mode=shadow.long_mode).describe()})",
        f"{money(split.short.capital)} short-term in the breakout; every fill crosses half the "
        f"{DEFAULT_ROUND_TRIP_PCT}% round trip.",
        "",
        f"  {'account':<30}{'capital':>10}{'equity':>11}{'return':>9}{'max dd':>9}{'in coins':>10}",
        _row(split.long.name, split.long),
        _row(split.short.name, split.short),
        _row(split.combined.name, split.combined),
        _row(result.hold.name, result.hold),
    ]

    open_short = [t for t in split.short.trades if t.open]
    held_long: dict[str, list[Any]] = {}
    for trade in split.long.trades:
        held_long.setdefault(trade.symbol, []).append(trade)
    if open_short or held_long:
        lines += ["", "  held now"]
    for trade in open_short:
        prepared = result.prepared[trade.symbol]
        closes = [b.close for b in prepared.daily if trade.entry_day <= b.start <= result.through]
        view = prepared.views.get(result.through)
        stop = (
            f", stop {format_decimal(round_money(result.rule.stop(max(closes), view)))}"
            if view is not None and closes
            else ""
        )
        lines.append(
            f"  {SHORT_TERM:<11} {trade.symbol:<9} bought {trade.entry_day:%Y-%m-%d} for "
            f"{money(trade.cost)}, worth {money(trade.proceeds)} at the bid{stop}"
        )
    for symbol, trades in sorted(held_long.items()):
        cost = sum((t.cost for t in trades), ZERO)
        worth = sum((t.proceeds for t in trades), ZERO)
        lines.append(
            f"  {LONG_TERM:<11} {symbol:<9} {len(trades)} tranche(s) for {money(cost)}, "
            f"worth {money(worth)} at the bid"
        )

    if result.fills:
        lines += ["", f"  paper fills ({len(result.fills)})"]
    for fill in result.fills:
        record = logged.get(fill.day.date().isoformat())
        note = "not logged live: rhca run did not record this day"
        if record is not None:
            match = next(
                (
                    f for f in record.get("fills") or []
                    if f"{f.get('sleeve')}|{f.get('symbol')}|{f.get('side')}" == fill.key
                ),
                None,
            )
            spread = (match or {}).get("spread_pct")
            note = (
                "NOT in rhca run's record for this day"
                if match is None
                else f"logged live, Robinhood spread {spread}%" if spread else "logged live"
            )
        lines.append(
            f"  {fill.day:%Y-%m-%d} {fill.sleeve:<11} {fill.side:<5}{fill.symbol:<9}"
            f"{money(fill.dollars):>10} at the {format_decimal(fill.close)} close  ({note})"
        )

    checks = check_bar(result, window, logged)
    done = result.closes >= BAR_DAYS
    lines += [
        "",
        f"  The bar (fixed 2026-09-28), read after {BAR_DAYS} closes"
        + ("" if done else f" -- {result.closes} so far, so every verdict is provisional") + ":",
    ]
    for number, check in enumerate(checks, start=1):
        verdict = "-" if check.passed is None else ("pass" if check.passed else "FAIL")
        if not done and check.passed is not None:
            verdict += " so far"
        lines.append(f"  {number}. {check.name}")
        lines.append(f"     {check.detail}  [{verdict}]")
    if done:
        lines += ["", conclusion(checks)]
    return "\n".join(lines)


def conclusion(checks: Sequence[Check]) -> str:
    failed = [c.name.split(":")[0] for c in checks if c.passed is False]
    unknown = [c.name.split(":")[0] for c in checks if c.passed is None]
    if failed:
        return (
            f"It fails {', '.join(failed)}: the split did not behave live as in its backtest. "
            "Do not trade it; find out why first."
        )
    if unknown:
        return f"Not yet judged: nothing to measure {', '.join(unknown)} by. Keep it running."
    return (
        "All three pass: the split behaved live as in its backtest. Trading it with real "
        "money is still the owner's decision, and needs the risk-limit changes in "
        "docs/strategy.md ('The split')."
    )


def report(
    config: AgentConfig, audit: AuditLog, *, fetch: Fetch | None = None, now: datetime | None = None
) -> str:
    """``rhca shadow``: fetch, replay, and render."""
    shadow = _shadow(config)
    now = now or utcnow()
    through = latest_close(now)
    if through < shadow.start:
        return (
            f"The forward test counts from the {shadow.start:%Y-%m-%d} daily close; there is "
            f"nothing to report until {shadow.start + DAY:%Y-%m-%d} 00:00 UTC."
        )
    daily = fetch_daily(shadow.symbols, days=history_days(config, now=now), fetch=fetch)
    missing = sorted(s for s, bars in daily.items() if not bars)
    if missing:
        raise AgentError(f"Coinbase returned no daily bars for {', '.join(missing)}")
    # Report through the last close every coin has.
    through = min(through, *(bars[-1].start for bars in daily.values()))
    result = replay(daily, config, through=through)
    bar_last = shadow.start + DAY * (BAR_DAYS - 1)
    window = result if through <= bar_last else replay(daily, config, through=bar_last)
    return render(result, window, config, logged_days(audit, config))


def describe_status(config: AgentConfig, payload: Mapping[str, Any] | None) -> list[str]:
    """``rhca status`` lines, from the snapshot the last recorded day wrote."""
    if config.shadow is None:
        return ["forward test   : off (no config/shadow.yaml)"]
    if not payload or payload.get("setup") != setup(config):
        return [
            f"forward test   : counting from the {config.shadow.start:%Y-%m-%d} close; nothing "
            "recorded yet -- `rhca run` records each UTC daily close (paper only)"
        ]
    accounts = payload.get("accounts") or {}

    def account(name: str) -> str:
        entry = accounts.get(name) or {}
        try:
            capital = to_decimal(entry["capital"], field="capital")
            equity = to_decimal(entry["equity"], field="equity")
        except (KeyError, AgentError):
            return f"{name} ?"
        change = (equity / capital - 1) * 100 if capital > ZERO else ZERO
        return f"{name} {money(equity)} ({pct(change)})"

    updated = payload.get("updated_at")
    try:
        age = f", updated {parse_timestamp(str(updated)):%Y-%m-%d %H:%M} UTC" if updated else ""
    except ValueError:
        age = ""
    held = ", ".join(payload.get("open") or []) or "nothing"
    return [
        f"forward test   : {payload.get('closes', '?')} close(s) since {payload.get('start')}, "
        f"through {payload.get('through')}{age} (paper only; `rhca shadow` for the report)",
        f"  {account('split')} = {account(LONG_TERM)} + {account(SHORT_TERM)}; "
        f"{account('hold')}; breakout holds {held}",
    ]
