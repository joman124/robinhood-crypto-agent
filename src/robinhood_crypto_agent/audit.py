"""The append-only audit log.

This file is the system of record. It is append-only JSONL, written once per
event and never rewritten, because the questions it has to answer after the
fact -- *what did the agent propose, what did a human approve, what actually
filled, and why was anything blocked* -- are worthless if the log can be
edited into agreement with the outcome.

It is also load-bearing, not merely descriptive: :func:`daily_activity` is what
the risk engine reads to enforce the daily notional and daily loss caps. That
means the caps survive a restart, a new shell, and a crashed session, because
they are derived from durable state rather than from anything held in memory.

Event kinds
-----------
``proposal``        a trade idea was generated (recorded whether or not it passed risk)
``execution``       an order was placed and its response recorded
``pnl_observation`` realized P&L read back from Robinhood for a date
``kill_switch``     the kill switch was engaged or released
``note``            free-text operator annotation
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .errors import AuditError
from .models import (
    ExecutionRecord,
    Proposal,
    ProposalStatus,
    parse_timestamp,
    utcnow,
)
from .numeric import ZERO, format_decimal, to_decimal

KIND_PROPOSAL = "proposal"
KIND_EXECUTION = "execution"
KIND_PNL = "pnl_observation"
KIND_KILL_SWITCH = "kill_switch"
KIND_NOTE = "note"

VALID_KINDS = frozenset(
    {KIND_PROPOSAL, KIND_EXECUTION, KIND_PNL, KIND_KILL_SWITCH, KIND_NOTE}
)


@dataclass(frozen=True)
class DailyActivity:
    """Everything the daily risk caps are computed from, for one UTC date."""

    day: date
    executed_notional: Decimal
    execution_count: int
    realized_pnl: Decimal
    proposal_count: int
    symbols_traded: tuple[str, ...]

    @property
    def realized_loss(self) -> Decimal:
        """Loss as a positive number; zero when the day is flat or up."""
        return -self.realized_pnl if self.realized_pnl < ZERO else ZERO

    def remaining_notional(self, cap: Decimal) -> Decimal:
        return max(ZERO, cap - self.executed_notional)

    def describe(self) -> str:
        return (
            f"{self.day.isoformat()}: {self.execution_count} execution(s), "
            f"${format_decimal(self.executed_notional)} notional, "
            f"realized P&L ${format_decimal(self.realized_pnl)}, "
            f"{self.proposal_count} proposal(s)"
        )


class AuditLog:
    """Append-only JSONL event log."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- writing ---------------------------------------------------------

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one event. Returns the written record."""
        if kind not in VALID_KINDS:
            raise AuditError(f"unknown audit event kind: {kind!r}")
        record = {
            "kind": kind,
            "recorded_at": utcnow().isoformat(),
            **payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")
        return record

    def record_proposal(
        self, proposal: Proposal, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Log a proposal -- including one the risk engine rejected.

        Rejected proposals are logged deliberately: a record of what the agent
        *wanted* to do and was stopped from doing is the main evidence that the
        risk controls are doing anything.

        ``extra`` carries annotations such as the escalation trigger's verdict
        and System 2's decision. The core fields are written after it, so an
        annotation can never overwrite what was actually proposed.
        """
        return self.append(
            KIND_PROPOSAL,
            {
                **(extra or {}),
                "proposal_id": proposal.proposal_id,
                "symbol": proposal.symbol,
                "side": proposal.side.value,
                "quantity": format_decimal(proposal.quantity),
                "reference_price": format_decimal(proposal.reference_price),
                "notional": format_decimal(proposal.notional),
                "status": proposal.status.value,
                "risk_passed": proposal.risk.passed,
                "risk_failures": [f.rule for f in proposal.risk.blocking_failures],
                "score": proposal.view.score,
                "confidence": proposal.view.confidence,
                "regime": proposal.view.regime.value,
                "spread_pct": (
                    format_decimal(proposal.spread_pct) if proposal.spread_pct is not None else None
                ),
                "proposal": proposal.to_dict(),
            },
        )

    def record_execution(self, record: ExecutionRecord) -> dict[str, Any]:
        """Log a real execution, as reported by the MCP order response."""
        return self.append(
            KIND_EXECUTION,
            {
                "proposal_id": record.proposal_id,
                "symbol": record.symbol,
                "side": record.side.value,
                "requested_quantity": format_decimal(record.requested_quantity),
                "filled_quantity": format_decimal(record.filled_quantity),
                "notional": format_decimal(record.notional),
                "order_id": record.order_id,
                "state": record.state,
                "ref_id": record.ref_id,
                "tranche_index": record.tranche_index,
                "overridden": record.overridden,
                "raw_response": record.raw_response,
            },
        )

    def record_pnl(self, day: date, realized_pnl: Decimal, source: str) -> dict[str, Any]:
        """Log realized P&L for a date, read back from Robinhood.

        The agent cannot compute realized P&L itself -- fills, fees and lot
        disposal all happen at Robinhood -- so the daily loss cap depends on
        this being recorded from a real ``get_realized_pnl`` response.
        """
        return self.append(
            KIND_PNL,
            {
                "day": day.isoformat(),
                "realized_pnl": format_decimal(realized_pnl),
                "source": source,
            },
        )

    def record_kill_switch(self, engaged: bool, reason: str) -> dict[str, Any]:
        return self.append(
            KIND_KILL_SWITCH, {"engaged": engaged, "reason": reason}
        )

    def record_note(self, message: str) -> dict[str, Any]:
        return self.append(KIND_NOTE, {"message": message})

    # -- reading ---------------------------------------------------------

    def events(self, *, kind: str | None = None) -> Iterator[dict[str, Any]]:
        """Yield events oldest first, optionally filtered by kind."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AuditError(
                        f"{self.path}:{line_number} is not valid JSON ({exc}). "
                        "The audit log is append-only and must not be hand-edited."
                    ) from exc
                if not isinstance(record, dict):
                    raise AuditError(f"{self.path}:{line_number} is not a JSON object")
                if kind is None or record.get("kind") == kind:
                    yield record

    def _event_day(self, record: dict[str, Any]) -> date | None:
        raw = record.get("recorded_at")
        if not raw:
            return None
        try:
            return parse_timestamp(str(raw)).date()
        except ValueError:
            return None

    def daily_activity(self, day: date | None = None) -> DailyActivity:
        """Aggregate one UTC day's activity, for the daily risk caps.

        Days are UTC rather than local: the cap has to mean the same thing
        regardless of where the operator is, and a local-midnight boundary
        would silently reset the cap mid-session for anyone travelling.
        """
        day = day or utcnow().date()
        executed_notional = ZERO
        execution_count = 0
        proposal_count = 0
        symbols: list[str] = []
        pnl_by_source: dict[str, Decimal] = {}

        for record in self.events():
            kind = record.get("kind")

            if kind == KIND_PNL:
                try:
                    event_day = date.fromisoformat(str(record.get("day", "")))
                except ValueError:
                    continue
                if event_day == day:
                    # Later observations for the same source supersede earlier
                    # ones -- realized P&L is a running total at Robinhood, not
                    # a per-event delta, so summing would double-count.
                    pnl_by_source[str(record.get("source", "unknown"))] = to_decimal(
                        record.get("realized_pnl", 0), field="realized_pnl"
                    )
                continue

            if self._event_day(record) != day:
                continue

            if kind == KIND_PROPOSAL:
                proposal_count += 1
            elif kind == KIND_EXECUTION:
                if str(record.get("state", "")).lower() in {"rejected", "failed", "voided"}:
                    # A rejected order moved no money, so it must not consume
                    # the daily notional budget.
                    continue
                execution_count += 1
                executed_notional += to_decimal(record.get("notional", 0), field="notional")
                symbol = record.get("symbol")
                if symbol:
                    symbols.append(str(symbol))

        return DailyActivity(
            day=day,
            executed_notional=executed_notional,
            execution_count=execution_count,
            realized_pnl=sum(pnl_by_source.values(), ZERO),
            proposal_count=proposal_count,
            symbols_traded=tuple(dict.fromkeys(symbols)),
        )

    def find_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """The most recent logged proposal with this id."""
        found = None
        for record in self.events(kind=KIND_PROPOSAL):
            if record.get("proposal_id") == proposal_id:
                found = record
        return found

    def executions_for(self, proposal_id: str) -> list[dict[str, Any]]:
        """Every execution recorded against a proposal.

        A staged plan fills over several tranches, each logged separately, so
        this returning multiple records is the intended accumulation behavior
        rather than a duplicate.
        """
        return [
            record
            for record in self.events(kind=KIND_EXECUTION)
            if record.get("proposal_id") == proposal_id
        ]

    def filled_quantity_for(self, proposal_id: str) -> Decimal:
        """Total quantity filled so far against a proposal."""
        total = ZERO
        for record in self.executions_for(proposal_id):
            if str(record.get("state", "")).lower() in {"rejected", "failed", "voided"}:
                continue
            total += to_decimal(record.get("filled_quantity", 0), field="filled_quantity")
        return total

    def recent(self, limit: int = 20, *, kind: str | None = None) -> list[dict[str, Any]]:
        records = list(self.events(kind=kind))
        return records[-limit:]


def proposal_status_for(passed: bool) -> ProposalStatus:
    return ProposalStatus.PROPOSED if passed else ProposalStatus.REJECTED_BY_RISK


def day_from(value: str | date | datetime | None) -> date:
    """Coerce a CLI date argument to a UTC date."""
    if value is None:
        return utcnow().date()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
