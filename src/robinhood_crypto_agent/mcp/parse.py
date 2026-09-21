"""Parsers for RobinHood MCP crypto tool responses.

Shapes are taken from live responses. Every tool wraps its payload as
``{"data": {"results": [...]}, "guide": "..."}``; ``guide`` is rendering advice
for a chat client and carries no data, so it is ignored here. A bare
``{"results": [...]}`` or a bare list is also accepted, because a caller may
reasonably hand this layer the inner object it already unwrapped.

Two response quirks are handled deliberately rather than defensively:

* ``get_crypto_quotes`` returns the symbol **unhyphenated** (``BTCUSD``) while
  ``get_currency_pairs`` returns it hyphenated (``BTC-USD``). Both are
  normalized to the canonical hyphenated form.
* A zero ``bid_price``/``ask_price`` means that side of the book is
  unavailable, and a zero ``mark_price`` means the mark is unusable -- these
  are *not* prices of zero. Such a quote is rejected rather than parsed into a
  number that would flow into a limit price.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from ..errors import AgentError
from ..models import (
    Account,
    ExecutionRecord,
    PairConstraints,
    Position,
    Quote,
    Side,
    decimal_field,
    parse_timestamp,
    utcnow,
)
from ..numeric import ZERO, round_money, to_decimal
from ..symbols import canonical


def unwrap_results(payload: Any) -> list[dict[str, Any]]:
    """Pull the ``results`` list out of whichever envelope was passed."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise AgentError(f"expected an MCP response object, got {type(payload).__name__}")

    node: Any = payload
    if isinstance(node.get("data"), (dict, list)):
        node = node["data"]
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    results = node.get("results")
    if results is None:
        # A single-object response (e.g. an order) rather than a collection.
        return [node]
    if not isinstance(results, list):
        raise AgentError("'results' must be a list")
    return [item for item in results if isinstance(item, dict)]


def next_cursor(payload: Any) -> str | None:
    """Extract the pagination cursor from a response's ``next`` URL.

    Callers pass this straight back as the ``cursor`` argument; the tools take
    the cursor value, not the whole URL.
    """
    if not isinstance(payload, dict):
        return None
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    nxt = node.get("next")
    if not nxt or not isinstance(nxt, str):
        return None
    from urllib.parse import parse_qs, urlparse

    values = parse_qs(urlparse(nxt).query).get("cursor")
    return values[0] if values else None


def _positive(value: Decimal | None) -> Decimal | None:
    """Treat a zero or negative price as absent, per the quote semantics."""
    if value is None or value <= ZERO:
        return None
    return value


def parse_quotes(payload: Any, *, observed_at: datetime | None = None) -> list[Quote]:
    """Parse ``get_crypto_quotes`` into :class:`Quote` objects.

    A row whose mark is unusable falls back to the mid of a two-sided book, and
    a row with neither is skipped -- there is no price to act on, and inventing
    one is how a bad limit price gets submitted.
    """
    quotes: list[Quote] = []
    for row in unwrap_results(payload):
        symbol_raw = row.get("symbol")
        if not symbol_raw:
            continue
        symbol = canonical(str(symbol_raw))

        bid = _positive(decimal_field(row, "bid_price", "bid"))
        ask = _positive(decimal_field(row, "ask_price", "ask"))
        mark = _positive(decimal_field(row, "mark_price", "mark"))

        if mark is None and bid is not None and ask is not None:
            mark = (bid + ask) / Decimal(2)
        if mark is None:
            continue
        # A one-sided book still yields a usable mark; fill the missing side
        # with the mark so spread math degrades to zero rather than exploding.
        bid = bid if bid is not None else mark
        ask = ask if ask is not None else mark

        timestamp = row.get("updated_at") or row.get("ask_time") or row.get("bid_time")
        try:
            when = parse_timestamp(timestamp) if timestamp else (observed_at or utcnow())
        except ValueError:
            when = observed_at or utcnow()

        quotes.append(
            Quote(
                symbol=symbol,
                bid=bid,
                ask=ask,
                mark=mark,
                observed_at=when,
                previous_close=_positive(decimal_field(row, "open_price", "previous_close")),
            )
        )
    return quotes


def parse_currency_pairs(payload: Any) -> list[PairConstraints]:
    """Parse ``get_currency_pairs`` into per-pair order constraints."""
    pairs: list[PairConstraints] = []
    for row in unwrap_results(payload):
        symbol_raw = row.get("symbol") or row.get("display_symbol")
        if not symbol_raw:
            continue
        increment = decimal_field(row, "min_order_quantity_increment")
        halted_regions = row.get("halted_regions") or []
        tradability = str(row.get("tradability", "tradable")).lower()
        pairs.append(
            PairConstraints(
                symbol=canonical(str(symbol_raw)),
                quantity_increment=increment if increment and increment > ZERO else Decimal("0.00000001"),
                min_order_size=decimal_field(row, "min_order_size"),
                max_order_size=decimal_field(row, "max_order_size"),
                price_increment=decimal_field(row, "min_order_price_increment"),
                min_notional=decimal_field(row, "min_order_quote_amount"),
                market_orders_only=bool(row.get("market_orders_only", False)),
                tradable=tradability == "tradable" and not row.get("display_only", False),
                halted=bool(row.get("halted", False)),
                halted_regions=tuple(str(r).upper() for r in halted_regions if r),
                pair_id=row.get("id"),
            )
        )
    return pairs


def parse_positions(payload: Any) -> list[Position]:
    """Parse ``get_crypto_positions``.

    Zero-quantity rows are dropped: Robinhood keeps a position record after a
    full exit, and counting those as open positions would consume the
    open-position risk cap with holdings that do not exist.
    """
    positions: list[Position] = []
    for row in unwrap_results(payload):
        currency = row.get("currency") or row.get("asset_currency") or {}
        code = None
        if isinstance(currency, dict):
            code = currency.get("code") or currency.get("symbol")
        symbol_raw = row.get("symbol") or code
        if not symbol_raw:
            continue
        quantity = decimal_field(row, "quantity", "quantity_available", "total_quantity")
        if quantity is None or quantity <= ZERO:
            continue
        positions.append(
            Position(
                symbol=canonical(str(symbol_raw)),
                quantity=quantity,
                cost_basis=decimal_field(row, "cost_basis", "direct_cost_basis", "total_cost_basis"),
            )
        )
    return positions


def parse_accounts(payload: Any) -> list[Account]:
    """Parse ``get_accounts``, keeping both account-number spellings distinct."""
    accounts: list[Account] = []
    for row in unwrap_results(payload):
        rhs = row.get("rhs_account_number")
        account_number = row.get("account_number")
        if not rhs and not account_number:
            continue
        accounts.append(
            Account(
                account_number=str(account_number or rhs),
                rhs_account_number=str(rhs or ""),
                buying_power=decimal_field(row, "buying_power"),
                crypto_buying_power=decimal_field(row, "crypto_buying_power"),
            )
        )
    return accounts


#: Order states that mean the order will not do anything further.
TERMINAL_STATES = frozenset({"filled", "canceled", "rejected", "failed", "voided"})
#: Order states that mean the order is still live at Robinhood.
OPEN_STATES = frozenset({"queued", "confirmed", "partially_filled"})


def parse_order_response(
    payload: Any,
    *,
    proposal_id: str,
    tranche_index: int | None = None,
    overridden: bool = False,
) -> ExecutionRecord:
    """Turn a ``place_crypto_order`` (or preview) response into an audit record.

    The filled quantity is read from the executions when present and falls back
    to the order's cumulative quantity, so a partial fill is recorded as the
    amount that actually traded rather than the amount requested.
    """
    rows = unwrap_results(payload)
    if not rows:
        raise AgentError("order response contained no result object")
    row = rows[0]

    symbol_raw = row.get("symbol") or (row.get("currency_pair") or {}).get("symbol")
    if not symbol_raw:
        raise AgentError("order response has no symbol")

    side_raw = str(row.get("side", "")).lower()
    if side_raw not in {"buy", "sell"}:
        raise AgentError(f"order response has an unrecognized side: {row.get('side')!r}")

    requested = decimal_field(row, "quantity", "asset_quantity") or ZERO
    filled = _filled_quantity(row)
    price = decimal_field(row, "average_price", "executed_price", "price", "limit_price")
    notional = decimal_field(row, "executed_notional", "total_notional", "dollar_amount")
    if isinstance(notional, dict):  # some notional fields arrive as {amount, currency}
        notional = to_decimal(notional.get("amount", 0), field="notional")
    if notional is None:
        # Derived from the fill, so round to cents: an un-rounded product of a
        # 8dp quantity and a 2dp price renders as a 9-decimal dollar amount.
        notional = round_money(filled * price) if price is not None else ZERO

    return ExecutionRecord(
        proposal_id=proposal_id,
        symbol=canonical(str(symbol_raw)),
        side=Side(side_raw),
        recorded_at=utcnow(),
        requested_quantity=requested,
        filled_quantity=filled,
        notional=notional,
        order_id=row.get("id") or row.get("order_id"),
        state=str(row.get("state", "unknown")).lower(),
        ref_id=row.get("ref_id"),
        tranche_index=tranche_index,
        overridden=overridden,
        raw_response=row,
    )


def _filled_quantity(row: dict[str, Any]) -> Decimal:
    executions: Iterable[Any] = row.get("executions") or []
    total = ZERO
    for execution in executions:
        if isinstance(execution, dict):
            quantity = decimal_field(execution, "quantity", "effective_quantity")
            if quantity is not None:
                total += quantity
    if total > ZERO:
        return total
    cumulative = decimal_field(row, "cumulative_quantity", "filled_asset_quantity")
    return cumulative if cumulative is not None else ZERO
