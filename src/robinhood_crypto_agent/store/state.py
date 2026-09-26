"""A cache of the last-seen Robinhood account and market state.

Each MCP tool response Claude fetches is ingested into this file, and the
analysis run reads it back. The indirection buys three things:

* **Commands compose.** Claude calls ``get_crypto_quotes``, then
  ``get_currency_pairs``, then ``get_crypto_positions`` -- in any order, across
  separate turns -- and ``analyze`` sees all of them.
* **Staleness is visible.** Every section records when it was written, so the
  report can say a position snapshot is two hours old rather than treating it
  as current.
* **Nothing is inferred.** A section that was never ingested reads as absent,
  and the risk engine downgrades the checks that depended on it instead of
  assuming a convenient default.

The file is rewritten atomically (write to a temp file, then replace) so an
interrupted write cannot leave a half-written state that parses as valid.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from ..models import (
    Account,
    PairConstraints,
    Position,
    Quote,
    parse_timestamp,
    utcnow,
)
from ..numeric import format_decimal, to_decimal
from ..symbols import canonical

SECTION_QUOTES = "quotes"
SECTION_PAIRS = "pairs"
SECTION_POSITIONS = "positions"
SECTION_PORTFOLIO = "portfolio"
SECTION_ACCOUNT = "account"
SECTION_CRYPTO_BUYING_POWER = "crypto_buying_power"


@dataclass(frozen=True)
class SectionAge:
    name: str
    updated_at: datetime | None

    @property
    def age_seconds(self) -> float | None:
        if self.updated_at is None:
            return None
        return (utcnow() - self.updated_at).total_seconds()

    def describe(self) -> str:
        if self.updated_at is None:
            return f"{self.name}: never ingested"
        age = self.age_seconds or 0.0
        return f"{self.name}: updated {age / 60:.1f} minutes ago"


class StateCache:
    """Reads and writes the cached market-state snapshot."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- persistence ------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        )
        try:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def _update_section(self, name: str, payload: Any) -> None:
        data = self._read()
        data[name] = {"updated_at": utcnow().isoformat(), "value": payload}
        self._write(data)

    def _section(self, name: str) -> tuple[Any, datetime | None]:
        section = self._read().get(name)
        if not isinstance(section, dict):
            return None, None
        updated_at = None
        raw = section.get("updated_at")
        if raw:
            try:
                updated_at = parse_timestamp(str(raw))
            except ValueError:
                updated_at = None
        return section.get("value"), updated_at

    # -- writes -----------------------------------------------------------

    def put_quotes(self, quotes: Iterable[Quote]) -> int:
        """Merge quotes into the cache, newest per symbol winning."""
        existing, _ = self._section(SECTION_QUOTES)
        merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        count = 0
        for quote in quotes:
            merged[quote.symbol] = {
                "bid": format_decimal(quote.bid),
                "ask": format_decimal(quote.ask),
                "mark": format_decimal(quote.mark),
                "observed_at": quote.observed_at.isoformat(),
                "previous_close": (
                    format_decimal(quote.previous_close)
                    if quote.previous_close is not None
                    else None
                ),
            }
            count += 1
        self._update_section(SECTION_QUOTES, merged)
        return count

    def put_pairs(self, pairs: Iterable[PairConstraints]) -> int:
        existing, _ = self._section(SECTION_PAIRS)
        merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        count = 0
        for pair in pairs:
            merged[pair.symbol] = {
                "quantity_increment": format_decimal(pair.quantity_increment),
                "min_order_size": _opt(pair.min_order_size),
                "max_order_size": _opt(pair.max_order_size),
                "price_increment": _opt(pair.price_increment),
                "min_notional": _opt(pair.min_notional),
                "market_orders_only": pair.market_orders_only,
                "tradable": pair.tradable,
                "halted": pair.halted,
                "halted_regions": list(pair.halted_regions),
                "pair_id": pair.pair_id,
            }
            count += 1
        self._update_section(SECTION_PAIRS, merged)
        return count

    def put_positions(self, positions: Iterable[Position]) -> int:
        """Replace the position snapshot wholesale.

        Positions are replaced rather than merged: a symbol missing from a
        fresh ``get_crypto_positions`` response means the position was closed,
        and merging would keep a sold-out holding alive forever.
        """
        payload = {
            position.symbol: {
                "quantity": format_decimal(position.quantity),
                "cost_basis": _opt(position.cost_basis),
            }
            for position in positions
        }
        self._update_section(SECTION_POSITIONS, payload)
        return len(payload)

    def put_portfolio_value(self, value: Decimal) -> None:
        self._update_section(SECTION_PORTFOLIO, format_decimal(value))

    def put_crypto_buying_power(self, value: Decimal) -> None:
        """What the account orders go to can spend on crypto, from ``get_portfolio``.

        Its own section, not a field of ``account``: ``get_accounts`` carries no
        reliable buying power, so ``ingest accounts`` must not overwrite it.
        """
        self._update_section(SECTION_CRYPTO_BUYING_POWER, format_decimal(value))

    def put_account(self, account: Account) -> None:
        self._update_section(
            SECTION_ACCOUNT,
            {
                "account_number": account.account_number,
                "rhs_account_number": account.rhs_account_number,
                "buying_power": _opt(account.buying_power),
                "crypto_buying_power": _opt(account.crypto_buying_power),
            },
        )

    # -- reads ------------------------------------------------------------

    def quotes(self) -> dict[str, Quote]:
        value, _ = self._section(SECTION_QUOTES)
        if not isinstance(value, dict):
            return {}
        out: dict[str, Quote] = {}
        for symbol, row in value.items():
            if not isinstance(row, dict):
                continue
            try:
                out[canonical(symbol)] = Quote(
                    symbol=canonical(symbol),
                    bid=to_decimal(row["bid"], field="bid"),
                    ask=to_decimal(row["ask"], field="ask"),
                    mark=to_decimal(row["mark"], field="mark"),
                    observed_at=parse_timestamp(str(row["observed_at"])),
                    previous_close=(
                        to_decimal(row["previous_close"], field="previous_close")
                        if row.get("previous_close") is not None
                        else None
                    ),
                )
            except (KeyError, ValueError, ArithmeticError):
                continue
        return out

    def pairs(self) -> dict[str, PairConstraints]:
        value, _ = self._section(SECTION_PAIRS)
        if not isinstance(value, dict):
            return {}
        out: dict[str, PairConstraints] = {}
        for symbol, row in value.items():
            if not isinstance(row, dict):
                continue
            try:
                out[canonical(symbol)] = PairConstraints(
                    symbol=canonical(symbol),
                    quantity_increment=to_decimal(
                        row.get("quantity_increment", "0.00000001"),
                        field="quantity_increment",
                    ),
                    min_order_size=_opt_decimal(row.get("min_order_size")),
                    max_order_size=_opt_decimal(row.get("max_order_size")),
                    price_increment=_opt_decimal(row.get("price_increment")),
                    min_notional=_opt_decimal(row.get("min_notional")),
                    market_orders_only=bool(row.get("market_orders_only", False)),
                    tradable=bool(row.get("tradable", True)),
                    halted=bool(row.get("halted", False)),
                    halted_regions=tuple(row.get("halted_regions") or ()),
                    pair_id=row.get("pair_id"),
                )
            except (ValueError, ArithmeticError):
                continue
        return out

    def positions(self) -> dict[str, Position]:
        value, _ = self._section(SECTION_POSITIONS)
        if not isinstance(value, dict):
            return {}
        out: dict[str, Position] = {}
        for symbol, row in value.items():
            if not isinstance(row, dict):
                continue
            quantity = _opt_decimal(row.get("quantity"))
            if quantity is None or quantity <= 0:
                continue
            out[canonical(symbol)] = Position(
                symbol=canonical(symbol),
                quantity=quantity,
                cost_basis=_opt_decimal(row.get("cost_basis")),
            )
        return out

    def portfolio_value(self) -> Decimal | None:
        value, _ = self._section(SECTION_PORTFOLIO)
        return _opt_decimal(value)

    def crypto_buying_power(self) -> Decimal | None:
        value, _ = self._section(SECTION_CRYPTO_BUYING_POWER)
        return _opt_decimal(value)

    def account(self) -> Account | None:
        value, _ = self._section(SECTION_ACCOUNT)
        if not isinstance(value, dict):
            return None
        return Account(
            account_number=str(value.get("account_number", "")),
            rhs_account_number=str(value.get("rhs_account_number", "")),
            buying_power=_opt_decimal(value.get("buying_power")),
            crypto_buying_power=_opt_decimal(value.get("crypto_buying_power")),
        )

    def ages(self) -> list[SectionAge]:
        """Freshness of every section, for the status report."""
        return [
            SectionAge(name, self._section(name)[1])
            for name in (
                SECTION_ACCOUNT,
                SECTION_QUOTES,
                SECTION_PAIRS,
                SECTION_POSITIONS,
                SECTION_PORTFOLIO,
                SECTION_CRYPTO_BUYING_POWER,
            )
        ]


def _opt(value: Decimal | None) -> str | None:
    return format_decimal(value) if value is not None else None


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return to_decimal(value)
    except Exception:
        return None
