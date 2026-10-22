"""The order desk: a proposal the owner accepted, sent to Robinhood and followed.

This is the only caller of :meth:`RobinhoodClient.place_order`, and it acts on
one thing: the owner accepting a proposal by its id on the dashboard (the
owner's decision, 2026-10-20; docs/autonomy.md). Nothing here decides to trade.

For each accept, in order -- any "no" ends it, is noted, and is pushed:

1. the accept is fresh (``MAX_DECISION_AGE``) and is the latest decision on
   that proposal;
2. the proposal is the split's, passed risk, is the newest for its coin, sleeve
   and rule (the loop re-logs a trade hourly under a new id), and was never sent;
3. :meth:`ApprovalGate.clear` on a fresh quote: kill switch, the approval names
   the id, no risk override (a button never can), price drift, remaining size;
4. a buy still fits the daily notional cap;
5. ``submitting`` is recorded *before* the POST, with an idempotency key derived
   from the proposal id, so a crash mid-send can only ever mean that one order.

Every cycle, each order it sent and not yet ended is read back from Robinhood
and any change recorded. Execution records carry deltas -- what filled since
the last record -- because the ledger and the daily caps sum them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..audit import KIND_EXECUTION, KIND_NOTE, KIND_PROPOSAL, AuditLog
from ..config import AgentConfig
from ..decisions import Decision, DecisionKind
from ..errors import AgentError
from ..ledger import (
    DEAD_STATES,
    FINAL_STATES,
    RULE_ENTRY,
    SHORT_TERM,
    STRATEGY_SPLIT,
    split_book,
    split_info,
)
from ..mcp.parse import parse_order_response
from ..models import (
    ExecutionRecord,
    PairConstraints,
    Proposal,
    ProposalStatus,
    Quote,
    Side,
    parse_timestamp,
    utcnow,
)
from ..net import HttpError
from ..numeric import ZERO, format_decimal, round_money, to_decimal
from ..serde import proposal_from_dict
from ..symbols import same_pair
from .gate import ApprovalGate
from .orders import api_order_body, client_order_id

log = logging.getLogger("rhca.desk")

#: Recorded before the POST, so an order whose response was lost is still held.
SUBMITTING = "submitting"
FAILED = "failed"

#: An accept older than this is not acted on: it describes a market that has
#: moved on, and a loop that was down when it was made must not act on it late.
MAX_DECISION_AGE = timedelta(minutes=15)
#: The loop re-logs what the split wants every hour, so a newer proposal is
#: always under this while the split still wants the trade.
MAX_PROPOSAL_AGE = timedelta(minutes=75)
#: An older stale accept is noted without a push, so a first start does not
#: push one alert per historical accept.
STALE_PUSH_WINDOW = timedelta(days=1)
#: How long a send with no clear answer is looked for before it is called failed.
#: ponytail: a created order still unlisted after this is booked as failed and
#: its slot released; the alert says to check the app. If that ever happens,
#: keep the slot held until a human clears it instead.
LOOKUP_GRACE = timedelta(minutes=30)
#: The order list is searched from this long before the send, for clock skew.
LOOKUP_SKEW = timedelta(minutes=2)

#: Every split proposal is one order.
TRANCHE = 0


class Client(Protocol):
    def best_bid_ask(self, symbols: Sequence[str]) -> list[Quote]: ...
    def place_order(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def get_order(self, order_id: str) -> dict[str, Any]: ...
    def find_order(
        self, symbol: str, client_order_id: str, *, since: datetime
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class Event:
    """Something the owner should hear about."""

    proposal_id: str
    title: str
    message: str


@dataclass
class _Sent:
    """One order the desk sent, as its execution records have it."""

    proposal_id: str
    symbol: str
    side: Side
    ref_id: str
    sent_at: datetime
    requested: Decimal
    state: str = SUBMITTING
    order_id: str | None = None
    filled: Decimal = ZERO
    notional: Decimal = ZERO


class OrderDesk:
    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLog,
        client: Client,
        *,
        pairs: Callable[[], Mapping[str, PairConstraints]] = dict,
        gate: ApprovalGate | None = None,
    ) -> None:
        self.config = config
        self.audit = audit
        self.client = client
        self.pairs = pairs
        self.gate = gate or ApprovalGate(config, audit=audit)

    # -- accepts --------------------------------------------------------------

    def act(self, decisions: Sequence[Decision]) -> list[Event]:
        """Send each new, latest accept's order, or say why not."""
        latest: dict[str, Decision] = {}
        for decision in decisions:
            current = latest.get(decision.proposal_id)
            if current is None or decision.decided_at > current.decided_at:
                latest[decision.proposal_id] = decision

        handled = self._handled()
        events: list[Event] = []
        for decision in latest.values():
            key = (decision.proposal_id, decision.decided_at.isoformat())
            if decision.kind is not DecisionKind.ACCEPT or key in handled:
                continue
            event = self._accept(decision)
            self.audit.append(
                KIND_NOTE,
                {
                    "message": f"order desk: {event.title} -- {event.message}",
                    "order_desk": {"proposal_id": key[0], "decided_at": key[1]},
                },
            )
            if utcnow() - decision.decided_at <= STALE_PUSH_WINDOW:
                events.append(event)
        return events

    def _accept(self, decision: Decision) -> Event:
        proposal_id = decision.proposal_id
        age = utcnow() - decision.decided_at
        if age > MAX_DECISION_AGE:
            return _refused(
                proposal_id,
                f"accepted {age.total_seconds() / 60:.0f} minutes ago, longer than the "
                f"{MAX_DECISION_AGE.total_seconds() / 60:.0f} minutes an accept stays good. "
                "Accept the current proposal again.",
            )
        record = self.audit.find_proposal(proposal_id)
        if record is None:
            return _refused(proposal_id, "no such proposal in the audit log")
        if record.get("strategy") != STRATEGY_SPLIT or record.get("status") != (
            ProposalStatus.PROPOSED.value
        ):
            return _refused(
                proposal_id, "only a split proposal that passed every risk rule can be sent"
            )
        latest = self._latest_for_slot(record)
        if latest != proposal_id:
            # The loop logs the same trade afresh each hour, under a new id.
            # Only the newest can be sent, so an older one accepted after it
            # can never be a second order for the same trade.
            return _refused(proposal_id, f"superseded by {latest}; accept that one instead")
        if self.audit.executions_for(proposal_id):
            return _refused(proposal_id, "an order was already sent for this proposal")

        try:
            proposal = proposal_from_dict(record["proposal"])
            if utcnow() - proposal.created_at > MAX_PROPOSAL_AGE:
                raise AgentError(
                    "this proposal is over an hour old, so the split no longer wants it "
                    "(it re-proposes a trade every hour while it does)"
                )
            working = self._check_book(record, proposal)
            quote = self._quote(proposal.symbol)
            self.gate.clear(
                proposal,
                approval_text=decision.approval_text,
                live_quote=quote,
                tranche_index=TRANCHE,
                allow_override=False,
            )
            if proposal.side is Side.BUY:
                self._check_daily_notional(proposal.notional, working)
            body = api_order_body(
                proposal, tranche_index=TRANCHE, constraints=self.pairs().get(proposal.symbol)
            )
        except (AgentError, KeyError, TypeError, ValueError) as exc:
            return _refused(proposal_id, str(exc))

        sent = _Sent(
            proposal_id=proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            ref_id=str(body["client_order_id"]),
            sent_at=utcnow(),
            requested=proposal.quantity,
        )
        what = f"{proposal.side.value} {format_decimal(proposal.quantity)} {proposal.symbol}"
        self.audit.record_execution(
            _record(sent, SUBMITTING, raw={"request": body}, overridden=False)
        )
        try:
            response = self.client.place_order(body)
        except HttpError as exc:
            if exc.retryable:
                return Event(
                    proposal_id,
                    "Order sent, no answer yet",
                    f"{what}: {exc}. Looking it up at Robinhood next cycle.",
                )
            self.audit.record_execution(_record(sent, FAILED, raw={"error": str(exc)}))
            return Event(proposal_id, "Order refused by Robinhood", f"{what}: {exc}")

        try:
            state = self._update(sent, response).state  # type: ignore[union-attr]
        except AgentError as exc:
            # Sent, but the answer is unreadable: still `submitting`, so the
            # next cycle finds the order by its id.
            state = f"something unreadable ({exc})"
        return Event(
            proposal_id,
            "Order placed",
            f"{what} limit ${_limit(body)} -- Robinhood says {state}.",
        )

    def _latest_for_slot(self, record: dict[str, Any]) -> str:
        """The newest split proposal for the same coin, sleeve and rule."""
        slot = slot_of(record)
        latest = str(record.get("proposal_id"))
        for other in self.audit.events(kind=KIND_PROPOSAL):
            if other.get("strategy") == STRATEGY_SPLIT and slot_of(other) == slot:
                latest = str(other.get("proposal_id"))
        return latest

    def _quote(self, symbol: str) -> Quote:
        quote = next(
            (q for q in self.client.best_bid_ask([symbol]) if same_pair(q.symbol, symbol)), None
        )
        if quote is None:
            raise AgentError(f"Robinhood returned no quote for {symbol}")
        return quote

    def _check_book(self, record: dict[str, Any], proposal: Proposal) -> Decimal:
        """What the split's books allow, rebuilt from the audit log: no second
        order on a coin and sleeve while one is working, no sell beyond what
        the short-term sleeve holds (never the long-term sleeve's coins, never
        the owner's own), no entry into a coin already held, no buy past the
        sleeve's cash. Returns the dollars working orders still have out."""
        info = split_info(record)
        if info is None:
            raise AgentError("not a split proposal")
        settings = self.config.strategy
        book = split_book(
            self.audit,
            long_capital=settings.split_long_capital,
            short_capital=settings.split_short_capital,
        )
        sleeve = book.sleeve(info.sleeve)
        holding = sleeve.holding(info.symbol)
        if holding.open_buy or holding.open_sell:
            raise AgentError(f"an order for {info.symbol} in the {info.sleeve} sleeve is working")
        if proposal.side is Side.SELL:
            if info.sleeve != SHORT_TERM or holding.quantity < proposal.quantity:
                raise AgentError(
                    f"the short-term sleeve holds {format_decimal(holding.quantity)} "
                    f"{info.symbol}, less than this sell"
                )
        else:
            if info.rule == RULE_ENTRY and holding.held:
                raise AgentError(f"the short-term sleeve already holds {info.symbol}")
            if sleeve.cash < proposal.notional:
                raise AgentError(
                    f"the {info.sleeve} sleeve has ${round_money(sleeve.cash)}, "
                    f"less than this ${round_money(proposal.notional)}"
                )
        return book.long.committed + book.short.committed

    def _check_daily_notional(self, notional: Decimal, working: Decimal) -> None:
        """Re-checked here: another order may have filled, or still be working,
        since the proposal was risk-checked."""
        cap = self.config.risk.max_daily_notional_usd
        used = self.audit.daily_activity().executed_notional + working
        if used + notional > cap:
            raise AgentError(
                f"${round_money(used)} traded or working today; this "
                f"${round_money(notional)} would pass the ${cap} daily cap"
            )

    def _handled(self) -> set[tuple[str, str]]:
        return {
            (str(n["order_desk"].get("proposal_id")), str(n["order_desk"].get("decided_at")))
            for n in self.audit.events(kind=KIND_NOTE)
            if isinstance(n.get("order_desk"), dict)
        }

    # -- following ------------------------------------------------------------

    def follow(self) -> list[Event]:
        """Read back every order sent and not yet ended; record what changed."""
        events: list[Event] = []
        for sent in self._sent().values():
            if sent.state in FINAL_STATES:
                continue
            try:
                event = self._follow_one(sent)
            except AgentError as exc:
                log.warning("could not read order for %s: %s", sent.proposal_id, exc)
                continue
            if event is not None:
                events.append(event)
        return events

    def _follow_one(self, sent: _Sent) -> Event | None:
        if sent.order_id is None:
            row = self.client.find_order(
                sent.symbol, sent.ref_id, since=sent.sent_at - LOOKUP_SKEW
            )
            if row is None:
                if utcnow() - sent.sent_at < LOOKUP_GRACE:
                    return None
                self.audit.record_execution(
                    _record(sent, FAILED, raw={"error": "Robinhood has no order for this id"})
                )
                return Event(
                    sent.proposal_id,
                    "Order not placed",
                    f"{sent.side.value} {sent.symbol}: the send got no answer, and "
                    f"{LOOKUP_GRACE.total_seconds() / 60:.0f} minutes later Robinhood lists no "
                    "order for it, so it is booked as never placed. Check the Robinhood app: "
                    "if the order is there after all, cancel it.",
                )
        else:
            row = self.client.get_order(sent.order_id)

        before = sent.state
        updated = self._update(sent, row)
        if updated is None or updated.state == before:
            return None
        verb = {"filled": "filled", "canceled": "canceled", "cancelled": "canceled"}.get(
            updated.state, updated.state.replace("_", " ")
        )
        return Event(
            sent.proposal_id,
            f"Order {verb}",
            f"{sent.side.value} {sent.symbol}: {format_decimal(sent.filled)} filled for "
            f"${round_money(sent.notional)}",
        )

    def _update(self, sent: _Sent, row: Any) -> ExecutionRecord | None:
        """Record what changed since the last record: state, and the fill delta."""
        reported = parse_order_response(row, proposal_id=sent.proposal_id, tranche_index=TRANCHE)
        filled = max(reported.filled_quantity - sent.filled, ZERO)
        notional = max(reported.notional - sent.notional, ZERO)
        if reported.state == sent.state and filled <= ZERO and sent.order_id:
            return None
        record = replace(
            reported,
            requested_quantity=sent.requested,
            filled_quantity=filled,
            notional=notional,
            ref_id=sent.ref_id,
        )
        if record.state in DEAD_STATES and filled > ZERO:
            # The books ignore a dead state's fills, so book what traded first.
            self.audit.record_execution(replace(record, state="partially_filled"))
            record = replace(record, filled_quantity=ZERO, notional=ZERO)
        self.audit.record_execution(record)
        sent.state, sent.order_id = record.state, record.order_id or sent.order_id
        sent.filled += filled
        sent.notional += notional
        return record

    def _sent(self) -> dict[str, _Sent]:
        """The desk's orders, rebuilt from the audit log: those whose ``ref_id``
        is the id the desk derives, so a hand-recorded MCP order is never read
        back against this API."""
        sent: dict[str, _Sent] = {}
        for record in self.audit.events(kind=KIND_EXECUTION):
            proposal_id = str(record.get("proposal_id", ""))
            if record.get("ref_id") != client_order_id(proposal_id, TRANCHE):
                continue
            try:
                order = sent.get(proposal_id) or _Sent(
                    proposal_id=proposal_id,
                    symbol=str(record["symbol"]),
                    side=Side(str(record["side"])),
                    ref_id=str(record["ref_id"]),
                    sent_at=parse_timestamp(str(record["recorded_at"])),
                    requested=to_decimal(record.get("requested_quantity", 0)),
                )
                state = str(record.get("state", "")).lower()
                if state not in DEAD_STATES:
                    order.filled += to_decimal(record.get("filled_quantity", 0))
                    order.notional += to_decimal(record.get("notional", 0))
            except (KeyError, ValueError, AgentError):
                continue
            order.state = state
            order.order_id = record.get("order_id") or order.order_id
            sent[proposal_id] = order
        return sent


def _record(
    sent: _Sent, state: str, *, raw: dict[str, Any], overridden: bool = False
) -> ExecutionRecord:
    return ExecutionRecord(
        proposal_id=sent.proposal_id,
        symbol=sent.symbol,
        side=sent.side,
        recorded_at=utcnow(),
        requested_quantity=sent.requested,
        filled_quantity=ZERO,
        notional=ZERO,
        order_id=sent.order_id,
        state=state,
        ref_id=sent.ref_id,
        tranche_index=TRANCHE,
        overridden=overridden,
        raw_response=raw,
    )


def slot_of(record: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (record.get("symbol"), record.get("sleeve"), record.get("rule"))


def _refused(proposal_id: str, why: str) -> Event:
    return Event(proposal_id, "Accept not sent", f"{proposal_id}: {why}")


def _limit(body: dict[str, Any]) -> str:
    config = body.get("limit_order_config")
    return str(config.get("limit_price")) if isinstance(config, dict) else "market"
