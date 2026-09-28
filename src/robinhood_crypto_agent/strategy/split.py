"""The split, live: what each sleeve does on one daily close.

A pure function of the day's indicators and the sleeves' state as the ledger
rebuilt it. It makes the same decisions ``portfolio_backtest.run_split`` makes
on the same bars, in the same order:

1. **Short-term stops.** A held breakout position whose close is more than
   3 x ATR(20) under its highest close since entry is sold, all of it.
2. **Short-term entries.** A coin the sleeve does not hold, closing above its
   prior 20-day high and its 100-day average, is bought: sized so the stop
   would lose 1% of the sleeve, at most 10% of the sleeve, and at most its
   cash.
3. **Long-term tranches.** A coin with tranches left, a week or more since its
   last, and -- bought the buy-low way -- closing under its 200-day average,
   gets one tranche.

No coin may be more than the per-coin limit of the whole account, counting
everything the account holds of it. The short-term sleeve gets the room
first: an entry is trimmed to fit, or turned away under the minimum trade,
and a tranche that does not fit waits.

Everything the rule declines to do is said, per coin, so "why is there no
proposal for ETH?" always has an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..ledger import LONG_TERM, RULE_ENTRY, RULE_STOP, RULE_TRANCHE, SHORT_TERM, SplitBook
from ..models import Side
from ..numeric import ZERO, format_decimal, round_money
from .breakout import Breakout, DayView
from .hodl import DIP, Accumulate

ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class CoinDay:
    """One coin's inputs on the daily close decided on."""

    symbol: str
    day: datetime
    close: Decimal
    #: The breakout's indicators; ``None`` before its 100 days of history.
    view: DayView | None
    #: The long-term 200-day average; ``None`` before its history.
    average: Decimal | None
    #: The highest close since the short-term position's entry day, today
    #: included; ``None`` when the sleeve holds none.
    highest: Decimal | None
    #: What one coin is worth now: holdings are valued at it.
    bid: Decimal
    #: What the whole account holds of the coin, in dollars.
    account_holding: Decimal = ZERO


@dataclass(frozen=True)
class SplitOrder:
    symbol: str
    sleeve: str
    rule: str
    side: Side
    day: datetime
    #: A buy's dollars, or a sell's quantity.
    dollars: Decimal | None = None
    quantity: Decimal | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    orders: list[SplitOrder] = field(default_factory=list)
    #: Per coin, what the rule did not do and why.
    notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, symbol: str, text: str) -> None:
        self.notes.setdefault(symbol, []).append(text)


def _p(value: Decimal) -> str:
    return f"{value:,.2f}" if abs(value) >= 1 else format_decimal(value)


def _money(value: Decimal) -> str:
    return f"${round_money(value)}"


def decide(
    coins: Mapping[str, CoinDay],
    book: SplitBook,
    rule: Breakout,
    plan: Accumulate,
    *,
    min_trade: Decimal,
    coin_cap_pct: Decimal | None,
    account_value: Decimal | None,
    long_symbols: Sequence[str],
) -> Decision:
    """Every order the split wants on this close, and why it wants no others.

    ``long_symbols`` are the coins the long-term sleeve's money is split
    across (the watchlist), whether or not each has a bar today.
    """
    decision = Decision()
    symbols = sorted(coins)
    short, long = book.short, book.long
    planned: dict[str, Decimal] = {}

    def room(symbol: str) -> Decimal | None:
        if coin_cap_pct is None or account_value is None or account_value <= ZERO:
            return None
        cap = coin_cap_pct / ONE_HUNDRED * account_value
        return cap - coins[symbol].account_holding - planned.get(symbol, ZERO)

    # What the short-term sleeve is worth now: its sizing base.
    equity = short.cash + sum(
        (
            h.quantity * coins[s].bid
            for s, h in short.holdings.items()
            if h.held and s in coins
        ),
        ZERO,
    )

    # 1. Stops.
    for symbol in symbols:
        coin, holding = coins[symbol], short.holdings.get(symbol)
        if holding is None or not holding.held:
            continue
        if coin.view is None or coin.highest is None:
            decision.note(symbol, "short-term: holding, but the stop's indicators are not ready")
            continue
        stop = rule.stop(coin.highest, coin.view)
        detail = _view_detail(coin) | {"highest": format_decimal(coin.highest),
                                       "stop": format_decimal(stop)}
        if not rule.exits(coin.highest, coin.view):
            decision.note(
                symbol,
                f"short-term: holding {format_decimal(holding.quantity)}; stop at {_p(stop)} "
                f"({rule.stop_atr} x ATR under the highest close {_p(coin.highest)})",
            )
            continue
        if holding.open_sell:
            decision.note(
                symbol,
                "short-term: a stop sell is still open; record its fill or its cancellation "
                "before another is proposed",
            )
            continue
        decision.orders.append(
            SplitOrder(
                symbol, SHORT_TERM, RULE_STOP, Side.SELL, coin.day,
                quantity=holding.quantity,
                reason=(
                    f"breakout stop: closed {_p(coin.close)}, under its stop {_p(stop)} "
                    f"({rule.stop_atr} x ATR {_p(coin.view.atr)} under the highest close "
                    f"{_p(coin.highest)} since entry); sell all {format_decimal(holding.quantity)}"
                ),
                detail=detail,
            )
        )

    # 2. Entries, each sized off the sleeve's equity, first in line for room.
    cash = short.cash
    for symbol in symbols:
        coin, holding = coins[symbol], short.holdings.get(symbol)
        if holding is not None and holding.held:
            continue
        if holding is not None and holding.open_buy:
            decision.note(
                symbol,
                "short-term: an entry order is still open; record its fill or its cancellation",
            )
            continue
        view = coin.view
        if view is None:
            decision.note(
                symbol, f"short-term: the breakout needs {rule.warmup_days + 1} daily closes"
            )
            continue
        if not rule.enters(view):
            decision.note(
                symbol,
                f"short-term: no breakout -- closed {_p(view.close)} against the prior "
                f"{rule.breakout_days}-day high {_p(view.prior_high)} and the "
                f"{rule.regime_days}-day average {_p(view.average)}",
            )
            continue
        wanted = min(rule.position_dollars(equity, view), cash)
        if wanted < min_trade:
            decision.note(
                symbol, f"short-term: a breakout, but only {_money(cash)} of the sleeve is free"
            )
            continue
        dollars, trimmed = wanted, None
        allowed = room(symbol)
        if allowed is not None and allowed < wanted:
            if allowed < min_trade:
                decision.note(
                    symbol,
                    f"short-term: a breakout, turned away -- the account already holds "
                    f"{_money(coin.account_holding)} of it, at the {coin_cap_pct}% per-coin limit",
                )
                continue
            dollars, trimmed = allowed, wanted
        cash -= dollars
        planned[symbol] = planned.get(symbol, ZERO) + dollars
        stop_fraction = rule.stop_fraction(view)
        decision.orders.append(
            SplitOrder(
                symbol, SHORT_TERM, RULE_ENTRY, Side.BUY, coin.day,
                dollars=dollars,
                reason=(
                    f"breakout entry: closed {_p(view.close)}, over its prior "
                    f"{rule.breakout_days}-day high {_p(view.prior_high)} and its "
                    f"{rule.regime_days}-day average {_p(view.average)}; buy {_money(dollars)}"
                    + (f" (trimmed from {_money(trimmed)} to the per-coin limit)" if trimmed else "")
                    + f", stop at {_p(view.close - rule.stop_atr * view.atr)}"
                ),
                detail=_view_detail(coin) | {
                    "dollars": format_decimal(round_money(dollars)),
                    "trimmed_from": format_decimal(round_money(trimmed)) if trimmed else None,
                    "stop": format_decimal(view.close - rule.stop_atr * view.atr),
                    "risk": format_decimal(round_money(dollars * stop_fraction)),
                },
            )
        )

    # 3. Long-term tranches, with the room the entries left.
    coin_count = len(long_symbols)
    share = long.capital / Decimal(coin_count) if coin_count else ZERO
    count = plan.tranches_for(share, min_trade) if coin_count else 0
    tranche = share / Decimal(count) if count else ZERO
    long_cash = long.cash
    for symbol in symbols:
        if symbol not in long_symbols:
            continue
        coin, holding = coins[symbol], long.holdings.get(symbol)
        bought = holding.tranches if holding else 0
        last = holding.last_buy_day if holding else None
        if bought >= count:
            decision.note(symbol, f"long-term: all {count} tranches bought; held")
            continue
        if not plan.due(day=coin.day, close=coin.close, average=coin.average, last_buy=last):
            decision.note(symbol, _not_due(plan, coin, last, bought, count))
            continue
        dollars = min(tranche, long_cash)
        if dollars <= ZERO or dollars < min_trade:
            decision.note(symbol, f"long-term: tranche due, but only {_money(long_cash)} is left")
            continue
        allowed = room(symbol)
        if allowed is not None and allowed < dollars:
            decision.note(
                symbol,
                f"long-term: tranche {bought + 1} of {count} waits -- no room under the "
                f"{coin_cap_pct}% per-coin limit",
            )
            continue
        long_cash -= dollars
        planned[symbol] = planned.get(symbol, ZERO) + dollars
        under = (
            f" under its {plan.average_days}-day average {_p(coin.average)}"
            if plan.mode == DIP and coin.average is not None
            else ""
        )
        decision.orders.append(
            SplitOrder(
                symbol, LONG_TERM, RULE_TRANCHE, Side.BUY, coin.day,
                dollars=dollars,
                reason=(
                    f"long-term {plan.label}: closed {_p(coin.close)}{under}; tranche "
                    f"{bought + 1} of {count}, {_money(dollars)}, held"
                ),
                detail={
                    "close": format_decimal(coin.close),
                    "average_200": format_decimal(coin.average) if coin.average else None,
                    "tranche": bought + 1,
                    "tranches": count,
                    "dollars": format_decimal(round_money(dollars)),
                    "mode": plan.mode,
                },
            )
        )
    return decision


def _view_detail(coin: CoinDay) -> dict[str, Any]:
    view = coin.view
    detail: dict[str, Any] = {"close": format_decimal(coin.close)}
    if view is not None:
        detail |= {
            "prior_high": format_decimal(view.prior_high),
            "average_100": format_decimal(view.average),
            "atr": format_decimal(view.atr),
        }
    return detail


def _not_due(
    plan: Accumulate, coin: CoinDay, last: datetime | None, bought: int, count: int
) -> str:
    progress = f"{bought} of {count} tranches bought"
    if last is not None and (coin.day - last).days < plan.every_days:
        return f"long-term: {progress}; the next waits a week after {last:%Y-%m-%d}"
    if coin.average is None:
        return f"long-term: {progress}; the {plan.average_days}-day average is not ready"
    return (
        f"long-term: {progress}; waits for a close under its {plan.average_days}-day "
        f"average {_p(coin.average)} (closed {_p(coin.close)})"
    )
