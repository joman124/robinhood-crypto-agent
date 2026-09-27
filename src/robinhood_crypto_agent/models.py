"""Domain types.

These are plain dataclasses rather than an ORM or pydantic models: everything
that crosses a boundary in this system is JSON on stdin/stdout, and keeping the
types dependency-free means the decision engine can be imported and unit-tested
without any of the trading machinery being reachable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from .numeric import ZERO, format_decimal, to_decimal


def utcnow() -> datetime:
    """Timezone-aware current UTC time.

    Naive datetimes are a recurring source of off-by-a-timezone bugs in daily
    risk caps, so every timestamp in this package is aware and in UTC.
    """
    return datetime.now(timezone.utc)


#: Fractional seconds, however many digits were sent.
_FRACTION = re.compile(r"\.(\d+)")


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    Fractional seconds are normalized to the six digits ``fromisoformat``
    accepts on every supported Python. Robinhood's Crypto API sends
    nanoseconds (``03:58:31.095125625Z``); rejected, those used to fall back to
    the fetch time silently, and a stale quote would then have read as fresh.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip().replace("Z", "+00:00")
        text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class JsonMixin:
    """Serialize a dataclass to JSON-safe primitives."""

    def to_dict(self) -> dict[str, Any]:
        return {k: _json_safe(v) for k, v in asdict(self).items()}  # type: ignore[call-overload]


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    """Order types the RobinHood crypto MCP tools accept.

    ``STOP_LOSS`` is a stop-triggered *market* order and applies to buy stops
    too; the user-facing name is "stop order", and ``STOP_LIMIT`` is a
    "stop limit order". The raw values here are tool inputs only.
    """

    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    GTC = "gtc"
    GFD = "gfd"
    GFW = "gfw"
    GFM = "gfm"


class ExecutionMode(str, Enum):
    """How far the agent is allowed to go on its own.

    ``PROPOSE_ONLY`` is Phase 1 and is what is implemented today. ``AUTO`` is
    Phase 2 -- the intended destination, not a dead end -- and is refused until
    the promotion criteria in ``docs/autonomy.md`` are met. It is a named value
    rather than an absence so that "is unattended trading on?" has an explicit
    answer in config, and so the gate is testable.

    Everything between a proposal and an order is already independent of *who*
    authorizes: sizing, the risk engine, the kill switch, the drift re-check,
    the remaining-quantity accounting and the audit log all run unattended.
    Only the approval step is human-shaped, so Phase 2 replaces one check
    rather than reworking the pipeline.
    """

    PROPOSE_ONLY = "propose_only"
    AUTO = "auto"


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    REJECTED_BY_RISK = "rejected_by_risk"
    #: The two below are System 1's, retired 2026-09-27. They stay so the
    #: records it left in the audit log still read, and so the approval gate
    #: can keep refusing them: neither was ever a proposal.
    #:
    #: Passed risk but not the escalation trigger, so System 2 never saw it.
    NOT_ESCALATED = "not_escalated"
    #: Escalated to System 2, which passed on it or failed to answer.
    DECLINED_BY_SYSTEM2 = "declined_by_system2"


@dataclass(frozen=True)
class Quote(JsonMixin):
    """One observation of a pair's price, from ``get_crypto_quotes``."""

    symbol: str
    bid: Decimal
    ask: Decimal
    mark: Decimal
    observed_at: datetime
    previous_close: Decimal | None = None

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        """Spread as a percentage of the mark; a liquidity/timing sanity check."""
        if self.mark == ZERO:
            return ZERO
        return self.spread / self.mark * Decimal(100)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)


@dataclass(frozen=True)
class Candle(JsonMixin):
    """An OHLC bar.

    Bars are *synthesized from observed quotes* (see ``store.prices``) because
    the RobinHood MCP server exposes no crypto historicals tool. ``observations``
    records how many quotes went into the bar, so a strategy can tell a
    well-sampled bar from one built out of a single stale tick.
    """

    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    observations: int = 1

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal(3)


@dataclass(frozen=True)
class PairConstraints(JsonMixin):
    """Per-pair order constraints from ``get_currency_pairs``.

    ``quantity_increment`` is the one that silently rejects orders: a quantity
    that is not a multiple of ``min_order_quantity_increment`` fails at
    placement, which can happen *after* a preview passed.
    ``market_orders_only`` is the other trap -- on such a pair, the limit orders
    a staged plan wants to submit are rejected outright.
    """

    symbol: str
    quantity_increment: Decimal
    min_order_size: Decimal | None = None
    max_order_size: Decimal | None = None
    price_increment: Decimal | None = None
    min_notional: Decimal | None = None
    market_orders_only: bool = False
    tradable: bool = True
    halted: bool = False
    halted_regions: tuple[str, ...] = ()
    pair_id: str | None = None

    @property
    def globally_halted(self) -> bool:
        """Whether the halt applies everywhere rather than to some regions.

        ``get_currency_pairs`` reports ``halted_regions: ["ALL"]`` for a global
        halt and a list of region codes otherwise. A regional halt may be
        one-sided (sell-only), so it is surfaced as a warning rather than
        treated as freely orderable.
        """
        return self.halted and "ALL" in self.halted_regions

    def supports(self, order_type: "OrderType") -> bool:
        """Whether this pair accepts the given order type."""
        if self.market_orders_only:
            return order_type is OrderType.MARKET
        return True

    @classmethod
    def permissive(cls, symbol: str) -> "PairConstraints":
        """A fallback used only where constraints were never fetched.

        Deliberately conservative on the increment (8dp, the finest Robinhood
        accepts) so a fallback cannot widen an order beyond what the real pair
        allows.
        """
        return cls(symbol=symbol, quantity_increment=Decimal("0.00000001"))


@dataclass(frozen=True)
class Position(JsonMixin):
    """An open crypto position from ``get_crypto_positions``."""

    symbol: str
    quantity: Decimal
    cost_basis: Decimal | None = None

    def market_value(self, mark: Decimal) -> Decimal:
        return self.quantity * mark


@dataclass(frozen=True)
class Account(JsonMixin):
    """Account identifiers from ``get_accounts``.

    The two numbers are genuinely different fields and mixing them up is the
    most likely way to send an order to the wrong place: order tools want
    ``rhs_account_number`` (numeric), while ``get_portfolio`` wants
    ``account_number`` (alphanumeric).
    """

    account_number: str
    rhs_account_number: str
    buying_power: Decimal | None = None
    crypto_buying_power: Decimal | None = None
    #: Whether this agent may trade the account. ``get_accounts`` lists every
    #: account, and exactly one is tradable by the agent: the Agentic one.
    agentic_allowed: bool | None = None


@dataclass(frozen=True)
class RiskFinding(JsonMixin):
    """One risk rule's verdict. ``blocking`` findings stop the proposal."""

    rule: str
    passed: bool
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class RiskDecision(JsonMixin):
    findings: list[RiskFinding]

    @property
    def passed(self) -> bool:
        return all(f.passed for f in self.findings if f.blocking)

    @property
    def blocking_failures(self) -> list[RiskFinding]:
        return [f for f in self.findings if f.blocking and not f.passed]

    @property
    def warnings(self) -> list[RiskFinding]:
        return [f for f in self.findings if not f.blocking and not f.passed]


@dataclass(frozen=True)
class Tranche(JsonMixin):
    """One slice of a staged entry."""

    index: int
    quantity: Decimal
    target_price: Decimal
    order_type: OrderType


@dataclass(frozen=True)
class ExecutionPlan(JsonMixin):
    """How an approved proposal should be filled.

    ``PROMPT`` means one order now; ``STAGED`` means one limit order per
    tranche, where an unfilled tranche is an expected outcome rather than
    something to chase by crossing the spread.
    """

    style: str
    tranches: list[Tranche]
    rationale: str

    @property
    def total_quantity(self) -> Decimal:
        return sum((t.quantity for t in self.tranches), ZERO)


@dataclass(frozen=True)
class Proposal(JsonMixin):
    """A single, reviewable trade idea -- the only thing a human can approve."""

    proposal_id: str
    symbol: str
    side: Side
    quantity: Decimal
    reference_price: Decimal
    notional: Decimal
    created_at: datetime
    #: Why the rule proposed it, in one line: which step, and the numbers.
    reason: str
    plan: ExecutionPlan
    risk: RiskDecision
    status: ProposalStatus
    #: How it was sized, and under ``"ladder"`` the rule's state when it was
    #: made: rule, step, anchor, close, trend average. The ledger rebuilds the
    #: ladder's cycles from it.
    sizing_detail: dict[str, Any] = field(default_factory=dict)
    #: The quote's bid/ask spread as a percentage of the mark, when proposed.
    spread_pct: Decimal | None = None

    @staticmethod
    def make_id(symbol: str, side: Side, quantity: Decimal, created_at: datetime) -> str:
        """A short, content-derived id.

        Deterministic rather than random so the same analysis run reproduces the
        same ids -- which makes an audit log diffable and a test assertable --
        while still being distinct per symbol, side, size, and second.
        """
        payload = "|".join(
            [
                symbol,
                side.value,
                format_decimal(quantity),
                created_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class OrderRequest(JsonMixin):
    """A validated payload for ``preview_crypto_order``/``place_crypto_order``.

    Constructed only by ``execution.orders``, which enforces the tool contract,
    so holding one of these means the payload was checked -- not that it was
    sent.
    """

    tool: str
    arguments: dict[str, Any]
    proposal_id: str
    tranche_index: int | None = None


@dataclass(frozen=True)
class ExecutionRecord(JsonMixin):
    """What actually happened when a human approved and Claude called the tool."""

    proposal_id: str
    symbol: str
    side: Side
    recorded_at: datetime
    requested_quantity: Decimal
    filled_quantity: Decimal
    notional: Decimal
    order_id: str | None
    state: str
    ref_id: str | None = None
    tranche_index: int | None = None
    overridden: bool = False
    raw_response: dict[str, Any] = field(default_factory=dict)


def decimal_field(data: dict[str, Any], *names: str) -> Decimal | None:
    """First present, non-null key among ``names``, as a Decimal.

    MCP responses are not guaranteed to use one spelling for a concept across
    every endpoint, so parsers accept the handful of names a field is known by
    rather than hard-failing on the first miss.
    """
    for name in names:
        if name in data and data[name] is not None:
            return to_decimal(data[name], field=name)
    return None
