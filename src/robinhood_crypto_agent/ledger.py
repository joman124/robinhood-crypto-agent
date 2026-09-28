"""What the split holds, read back from the audit log.

The split's rules (``strategy/split.py``) are stateless. What they need to
know -- each sleeve's cash, what it holds of each coin, when the short-term
position was entered, how many long-term tranches are bought -- is rebuilt
here from the audit log on every analysis, so it survives a restart and can
never drift from the record of what actually filled.

Only the split's own orders count. An execution is the split's when the
proposal it was recorded against carries split detail. A coin bought outside
the agent, by the retired trend ladder, or by System 1, is not in either
sleeve and is never proposed for sale -- though it still counts toward the
per-coin limit, which reads the whole account.

Orders
------
An order *holds its place* once it has filled any quantity, or is still open:
recorded, and not yet reported filled or ended. So the loop never proposes an
entry, a stop or a tranche twice while the first order is still working.
Record the order's final state from ``get_crypto_orders`` to release one that
was canceled unfilled. An open buy's unfilled dollars are held out of the
sleeve's cash until it ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .audit import KIND_EXECUTION, KIND_PROPOSAL, AuditLog
from .errors import AgentError
from .models import Side, parse_timestamp
from .numeric import ZERO, to_decimal
from .symbols import canonical

#: The ``strategy`` annotation on every proposal the split makes, and the key
#: its detail sits under in ``sizing_detail``.
STRATEGY_SPLIT = "split"

LONG_TERM = "long-term"
SHORT_TERM = "short-term"
SLEEVES = (LONG_TERM, SHORT_TERM)

#: What each order is: a breakout entry or stop, or a long-term tranche.
RULE_ENTRY = "entry"
RULE_STOP = "stop"
RULE_TRANCHE = "tranche"

#: States that moved nothing -- the same set the daily caps ignore. A canceled
#: order keeps what it filled.
DEAD_STATES = frozenset({"rejected", "failed", "voided"})
#: States after which an order can fill nothing more.
FINAL_STATES = DEAD_STATES | {"filled", "canceled", "cancelled"}


@dataclass(frozen=True)
class SplitInfo:
    """The split detail a proposal was made with."""

    symbol: str
    side: Side
    sleeve: str
    rule: str
    #: The daily close the rule decided on.
    day: datetime
    #: What the proposal expected to spend (a buy) or receive (a sell).
    notional: Decimal
    reference_price: Decimal


def split_info(record: dict[str, Any]) -> SplitInfo | None:
    """The split detail on a proposal record, or ``None`` if it is not the split's."""
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    detail = (proposal.get("sizing_detail") or {}).get(STRATEGY_SPLIT)
    if record.get("strategy") != STRATEGY_SPLIT or not isinstance(detail, dict):
        return None
    try:
        sleeve = str(detail["sleeve"])
        if sleeve not in SLEEVES:
            return None
        return SplitInfo(
            symbol=canonical(str(record["symbol"])),
            side=Side(str(record["side"])),
            sleeve=sleeve,
            rule=str(detail["rule"]),
            day=parse_timestamp(str(detail["day"])),
            notional=to_decimal(record.get("notional", 0), field="notional"),
            reference_price=to_decimal(record.get("reference_price", 0), field="reference_price"),
        )
    except (KeyError, ValueError, TypeError, AgentError):
        return None


@dataclass
class Holding:
    """One sleeve's position in one coin."""

    symbol: str
    sleeve: str
    quantity: Decimal = ZERO
    #: What the quantity held cost, spread included (first in, first out).
    cost: Decimal = ZERO
    #: Short-term: the daily close the held position was entered on.
    entry_day: datetime | None = None
    #: Long-term: tranches filled or still open, and the latest one's day.
    tranches: int = 0
    last_buy_day: datetime | None = None
    #: A buy or a sell of it is recorded and still open.
    open_buy: bool = False
    open_sell: bool = False
    last_fill_at: datetime | None = None
    #: Proceeds less cost over everything sold.
    realized_pnl: Decimal = ZERO
    _lots: list[tuple[Decimal, Decimal]] = field(default_factory=list, repr=False)

    @property
    def held(self) -> bool:
        return self.quantity > ZERO


@dataclass
class Sleeve:
    """One sleeve's money: what it started with, spent and got back."""

    name: str
    capital: Decimal
    spent: Decimal = ZERO
    received: Decimal = ZERO
    #: Open buys' unfilled dollars, held out of the cash until they end.
    committed: Decimal = ZERO
    holdings: dict[str, Holding] = field(default_factory=dict)

    @property
    def cash(self) -> Decimal:
        return self.capital - self.spent + self.received - self.committed

    @property
    def realized_pnl(self) -> Decimal:
        return sum((h.realized_pnl for h in self.holdings.values()), ZERO)

    def holding(self, symbol: str) -> Holding:
        symbol = canonical(symbol)
        return self.holdings.setdefault(symbol, Holding(symbol, self.name))


@dataclass
class SplitBook:
    long: Sleeve
    short: Sleeve

    def sleeve(self, name: str) -> Sleeve:
        return self.long if name == LONG_TERM else self.short


@dataclass
class _Order:
    info: SplitInfo
    state: str = ""
    filled: Decimal = ZERO
    notional: Decimal = ZERO

    @property
    def open(self) -> bool:
        return self.state not in FINAL_STATES

    @property
    def taken(self) -> bool:
        return self.filled > ZERO or self.open


def split_book(
    audit: AuditLog, *, long_capital: Decimal, short_capital: Decimal
) -> SplitBook:
    """Both sleeves, as the recorded fills of the split's orders have them."""
    book = SplitBook(Sleeve(LONG_TERM, long_capital), Sleeve(SHORT_TERM, short_capital))
    infos: dict[str, SplitInfo] = {}
    orders: dict[str, _Order] = {}
    for record in audit.events():
        kind = record.get("kind")
        if kind == KIND_PROPOSAL:
            found = split_info(record)
            if found is not None:
                infos[str(record.get("proposal_id"))] = found
        elif kind == KIND_EXECUTION:
            proposal_id = str(record.get("proposal_id", ""))
            info = infos.get(proposal_id)
            if info is not None:
                order = orders.setdefault(proposal_id, _Order(info))
                _apply(book, order, record)

    for order in orders.values():
        info = order.info
        holding = book.sleeve(info.sleeve).holding(info.symbol)
        if order.open:
            if info.side is Side.BUY:
                holding.open_buy = True
                book.sleeve(info.sleeve).committed += max(info.notional - order.notional, ZERO)
            else:
                holding.open_sell = True
        if info.rule == RULE_TRANCHE and order.taken:
            holding.tranches += 1
            if holding.last_buy_day is None or info.day > holding.last_buy_day:
                holding.last_buy_day = info.day
    return book


def _apply(book: SplitBook, order: _Order, record: dict[str, Any]) -> None:
    """One execution record, in the order it was recorded."""
    info = order.info
    state = str(record.get("state", "")).lower()
    try:
        at = parse_timestamp(str(record["recorded_at"]))
        filled = to_decimal(record.get("filled_quantity", 0), field="filled_quantity")
        notional = to_decimal(record.get("notional", 0), field="notional")
    except (KeyError, ValueError, AgentError):
        return
    order.state = state
    if state in DEAD_STATES or filled <= ZERO:
        return
    if notional <= ZERO:
        # A fill recorded without its dollars: value it at the proposed price.
        notional = filled * info.reference_price
    order.filled += filled
    order.notional += notional

    sleeve = book.sleeve(info.sleeve)
    holding = sleeve.holding(info.symbol)
    holding.last_fill_at = at
    if info.side is Side.BUY:
        if not holding.held and info.sleeve == SHORT_TERM:
            holding.entry_day = info.day
        sleeve.spent += notional
        holding.quantity += filled
        holding.cost += notional
        holding._lots.append((filled, notional / filled))
        return

    sleeve.received += notional
    price = notional / filled
    remaining = min(filled, holding.quantity)
    while holding._lots and remaining > ZERO:
        quantity, unit = holding._lots[0]
        take = min(quantity, remaining)
        holding.realized_pnl += take * (price - unit)
        holding.cost -= take * unit
        remaining -= take
        if take >= quantity:
            holding._lots.pop(0)
        else:
            holding._lots[0] = (quantity - take, unit)
    holding.quantity = max(holding.quantity - filled, ZERO)
    if not holding.held:
        holding.cost = ZERO
        holding._lots.clear()
        holding.entry_day = None
