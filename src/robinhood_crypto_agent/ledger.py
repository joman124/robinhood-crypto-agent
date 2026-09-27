"""What the ladder holds, read back from the audit log.

The rule in :mod:`strategy.ladder` is stateless. Everything it needs to know --
what is held, the cycle's anchor, which steps were bought and sold -- is
rebuilt here from the audit log on every analysis, so it survives a restart
and can never drift from the record of what actually filled.

Only the ladder's own orders count. An execution is the ladder's when the
proposal it was recorded against carries ladder detail; a coin bought outside
the agent, or by the retired System 1, is not in this ledger and is never
proposed for sale.

A cycle
-------
A cycle opens with the first ladder buy recorded after the position was
empty, and takes the anchor that buy was proposed with. It closes when a
recorded sell brings the ladder's holding back to zero. A buy order that
never filled and has ended (canceled, rejected) does not keep a cycle open.

Steps
-----
A step is *taken* once an order for it has filled any quantity, or is still
open: recorded, and not yet reported filled or ended. So the loop never
proposes a step twice while its first order is still working. Record the
order's final state from ``get_crypto_orders`` to release a step whose order
was canceled unfilled.
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

#: The ``strategy`` annotation on every proposal the ladder makes.
STRATEGY_LADDER = "ladder"

#: States that moved nothing, so they neither open nor close a lot -- the same
#: set the daily caps ignore. A canceled order keeps what it filled.
DEAD_STATES = frozenset({"rejected", "failed", "voided"})
#: States after which an order can fill nothing more.
FINAL_STATES = DEAD_STATES | {"filled", "canceled", "cancelled"}


@dataclass(frozen=True)
class LadderInfo:
    """The ladder detail a proposal was made with."""

    symbol: str
    side: Side
    rule: str
    step: int | None
    anchor: Decimal | None


@dataclass(frozen=True)
class OpenLot:
    """Quantity a recorded ladder buy added and no recorded sell has closed yet."""

    symbol: str
    quantity: Decimal
    filled_at: datetime
    proposal_id: str
    #: What each unit cost, from the fill's recorded notional; ``None`` when
    #: the fill recorded no notional.
    price: Decimal | None = None


@dataclass(frozen=True)
class LadderPosition:
    """One symbol's ladder state, as the audit log has it."""

    symbol: str
    lots: tuple[OpenLot, ...] = ()
    #: The open cycle's anchor, or ``None`` between cycles.
    anchor: Decimal | None = None
    cycle_started_at: datetime | None = None
    bought: frozenset[int] = frozenset()
    sold: frozenset[int] = frozenset()
    #: A ladder sell order is recorded and still open.
    open_sell: bool = False
    #: When the last cycle closed. ``None`` if one never has.
    flat_since: datetime | None = None
    last_fill_at: datetime | None = None
    #: Proceeds less FIFO cost over everything sold, where both are known.
    realized_pnl: Decimal = ZERO

    @property
    def held(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), ZERO)

    @property
    def in_cycle(self) -> bool:
        return self.anchor is not None

    @property
    def cost(self) -> Decimal | None:
        """What the open lots cost, or ``None`` if any fill lacks a notional."""
        if any(lot.price is None for lot in self.lots):
            return None
        return sum((lot.quantity * lot.price for lot in self.lots if lot.price), ZERO)

    @property
    def entry_proposal_ids(self) -> list[str]:
        return list(dict.fromkeys(lot.proposal_id for lot in self.lots))


def ladder_info(record: dict[str, Any]) -> LadderInfo | None:
    """The ladder detail on a proposal record, or ``None`` if it is not the ladder's."""
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    detail = (proposal.get("sizing_detail") or {}).get(STRATEGY_LADDER)
    if record.get("strategy") != STRATEGY_LADDER or not isinstance(detail, dict):
        return None
    try:
        side = Side(str(record["side"]))
        step = detail.get("step")
        anchor = detail.get("anchor")
        return LadderInfo(
            symbol=canonical(str(record["symbol"])),
            side=side,
            rule=str(detail.get("rule", "")),
            step=int(step) if step is not None else None,
            anchor=to_decimal(anchor, field="anchor") if anchor is not None else None,
        )
    except (KeyError, ValueError, TypeError, AgentError):
        return None


@dataclass
class _Order:
    info: LadderInfo
    state: str = ""
    filled: Decimal = ZERO

    @property
    def open(self) -> bool:
        return self.state not in FINAL_STATES

    @property
    def taken(self) -> bool:
        return self.filled > ZERO or self.open


@dataclass
class _Cycle:
    anchor: Decimal | None
    started_at: datetime
    orders: list[str] = field(default_factory=list)


class _Tracker:
    """Replays one symbol's ladder executions in the order they were recorded."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.lots: list[OpenLot] = []
        self.orders: dict[str, _Order] = {}
        self.cycle: _Cycle | None = None
        self.flat_since: datetime | None = None
        self.last_fill_at: datetime | None = None
        self.realized = ZERO

    @property
    def held(self) -> Decimal:
        return sum((lot.quantity for lot in self.lots), ZERO)

    def _cycle_alive(self) -> bool:
        if self.cycle is None:
            return False
        return self.held > ZERO or any(self.orders[pid].open for pid in self.cycle.orders)

    def apply(self, proposal_id: str, info: LadderInfo, record: dict[str, Any]) -> None:
        state = str(record.get("state", "")).lower()
        try:
            at = parse_timestamp(str(record["recorded_at"]))
            filled = to_decimal(record.get("filled_quantity", 0), field="filled_quantity")
        except (KeyError, ValueError, AgentError):
            return
        if state in DEAD_STATES:
            filled = ZERO

        if info.side is Side.BUY and not self._cycle_alive():
            self.cycle = _Cycle(anchor=info.anchor, started_at=at)
        order = self.orders.setdefault(proposal_id, _Order(info))
        order.state = state
        order.filled += max(filled, ZERO)
        if self.cycle is not None and proposal_id not in self.cycle.orders:
            self.cycle.orders.append(proposal_id)
        if filled <= ZERO:
            return

        self.last_fill_at = at
        price = _unit_price(record, filled)
        if info.side is Side.BUY:
            self.lots.append(OpenLot(self.symbol, filled, at, proposal_id, price))
            return
        self._consume(filled, price)
        if self.held <= ZERO:
            self.lots = []
            self.cycle = None
            self.flat_since = at

    def _consume(self, quantity: Decimal, price: Decimal | None) -> None:
        """Close ``quantity`` against the oldest lots first."""
        remaining = quantity
        while self.lots and remaining > ZERO:
            oldest = self.lots[0]
            take = min(oldest.quantity, remaining)
            if price is not None and oldest.price is not None:
                self.realized += take * (price - oldest.price)
            remaining -= take
            if take >= oldest.quantity:
                self.lots.pop(0)
            else:
                self.lots[0] = OpenLot(
                    oldest.symbol,
                    oldest.quantity - take,
                    oldest.filled_at,
                    oldest.proposal_id,
                    oldest.price,
                )

    def position(self) -> LadderPosition:
        alive = self._cycle_alive()
        cycle = self.cycle if alive else None
        orders = [self.orders[pid] for pid in cycle.orders] if cycle else []
        return LadderPosition(
            symbol=self.symbol,
            lots=tuple(self.lots),
            anchor=cycle.anchor if cycle else None,
            cycle_started_at=cycle.started_at if cycle else None,
            bought=frozenset(
                o.info.step
                for o in orders
                if o.info.side is Side.BUY and o.info.step is not None and o.taken
            ),
            sold=frozenset(
                o.info.step
                for o in orders
                if o.info.side is Side.SELL and o.info.step is not None and o.taken
            ),
            open_sell=any(o.info.side is Side.SELL and o.open for o in self.orders.values()),
            flat_since=self.flat_since,
            last_fill_at=self.last_fill_at,
            realized_pnl=self.realized,
        )


def _unit_price(record: dict[str, Any], filled: Decimal) -> Decimal | None:
    try:
        notional = to_decimal(record.get("notional", 0), field="notional")
    except (AgentError, ValueError, ArithmeticError):
        return None
    return notional / filled if notional > ZERO and filled > ZERO else None


def ladder_positions(audit: AuditLog) -> dict[str, LadderPosition]:
    """Every symbol the ladder has ever ordered, with its state now."""
    info: dict[str, LadderInfo] = {}
    trackers: dict[str, _Tracker] = {}
    for record in audit.events():
        kind = record.get("kind")
        if kind == KIND_PROPOSAL:
            found = ladder_info(record)
            if found is not None:
                info[str(record.get("proposal_id"))] = found
        elif kind == KIND_EXECUTION:
            proposal_id = str(record.get("proposal_id", ""))
            found = info.get(proposal_id)
            if found is None:
                continue  # not the ladder's order
            tracker = trackers.setdefault(found.symbol, _Tracker(found.symbol))
            tracker.apply(proposal_id, found, record)
    return {symbol: tracker.position() for symbol, tracker in trackers.items()}


def ladder_position(audit: AuditLog, symbol: str) -> LadderPosition:
    symbol = canonical(symbol)
    return ladder_positions(audit).get(symbol) or LadderPosition(symbol)
