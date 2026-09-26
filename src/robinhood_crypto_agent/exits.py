"""The time exit: sell what the agent bought once it has been held the horizon.

Every buy is graded on where the price is ``exit_after_bars`` bars later, and
the time exit makes the real trade match that grade: when a lot the agent
bought reaches that age, the pipeline proposes selling it. The proposal goes
through the same risk engine and the same approve-by-id gate as any other, so
nothing here places an order.

What counts as the agent's is read from the audit log, not from the account:
filled buys, less filled sells, first in first out. A coin bought outside the
agent never has a lot here, so the time exit never proposes selling it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .audit import KIND_EXECUTION, AuditLog
from .errors import AgentError
from .models import parse_timestamp
from .numeric import ZERO, to_decimal
from .symbols import canonical

#: Execution states that moved nothing, so they neither open nor close a lot --
#: the same set the daily caps ignore. A canceled order keeps what it filled.
DEAD_STATES = frozenset({"rejected", "failed", "voided"})

EXIT_TIME = "time"


@dataclass(frozen=True)
class OpenLot:
    """Quantity a recorded agent buy added and no recorded sell has closed yet."""

    symbol: str
    quantity: Decimal
    filled_at: datetime
    proposal_id: str


def open_lots(audit: AuditLog) -> dict[str, list[OpenLot]]:
    """The agent's open lots per symbol, oldest first.

    A fill's time is when it was recorded: the runbook has the fill recorded
    from ``get_crypto_orders`` once it happens, so this runs a little late at
    worst, never early.
    """
    lots: dict[str, list[OpenLot]] = {}
    for record in audit.events(kind=KIND_EXECUTION):
        quantity = _filled(record)
        if quantity is None:
            continue
        symbol = canonical(str(record.get("symbol", "")))
        side = str(record.get("side", "")).lower()
        if side == "buy":
            try:
                filled_at = parse_timestamp(str(record["recorded_at"]))
            except (KeyError, ValueError):
                continue
            lots.setdefault(symbol, []).append(
                OpenLot(symbol, quantity, filled_at, str(record.get("proposal_id", "")))
            )
        elif side == "sell":
            _consume(lots.get(symbol, []), quantity)
    return {symbol: held for symbol, held in lots.items() if held}


def due_lots(
    lots: dict[str, list[OpenLot]], *, now: datetime, hold: timedelta
) -> dict[str, list[OpenLot]]:
    """The lots held at least ``hold``, per symbol."""
    due: dict[str, list[OpenLot]] = {}
    for symbol, held in lots.items():
        ready = [lot for lot in held if now - lot.filled_at >= hold]
        if ready:
            due[symbol] = ready
    return due


def _filled(record: dict[str, Any]) -> Decimal | None:
    """The filled quantity of a live execution record, or ``None``."""
    state = str(record.get("state", "")).lower()
    try:
        quantity = to_decimal(record.get("filled_quantity", 0), field="filled_quantity")
    except (AgentError, ValueError, ArithmeticError):
        return None
    if quantity <= ZERO or state in DEAD_STATES:
        return None
    return quantity


def _consume(lots: list[OpenLot], quantity: Decimal) -> None:
    """Close ``quantity`` against the oldest lots first, in place."""
    remaining = quantity
    while lots and remaining > ZERO:
        oldest = lots[0]
        if oldest.quantity <= remaining:
            remaining -= oldest.quantity
            lots.pop(0)
        else:
            lots[0] = OpenLot(
                oldest.symbol, oldest.quantity - remaining, oldest.filled_at, oldest.proposal_id
            )
            remaining = ZERO
