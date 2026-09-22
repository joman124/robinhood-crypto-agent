"""Building the dashboard payload, and (optionally) pushing it.

The network boundary
--------------------
:func:`build_payload` is pure — it reads local files and returns a dict, and is
what the tests exercise. :func:`push` and :func:`fetch_decisions` talk only to a
dashboard URL the operator configures. If you would rather nothing here opened
a socket, use ``rhca dashboard-export`` to write the JSON and push it with curl;
nothing depends on :func:`push`.

What is deliberately **not** in the payload
-------------------------------------------
Account numbers, buying power, portfolio value, position sizes, order ids — and
System 2's written rationale, because Sonnet reads the holdings and may quote
them. The dashboard's job is to show what the agent *suggested* and whether
those suggestions were any good. It does not need to know how much money is
behind them, and a dashboard that never receives that data cannot leak it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from . import net
from .audit import KIND_NOTE, KIND_PROPOSAL, AuditLog
from .config import AgentConfig
from .decisions import Decision, decision_from_dict
from .errors import AgentError
from .models import ProposalStatus, utcnow
from .numeric import round_money
from .outcomes import (
    DEFAULT_HORIZON_BARS,
    DEFAULT_HURDLE_PCT,
    Outcome,
    aggregate,
    group_by,
    outcome_from_proposal_record,
)
from .store import PriceStore

PAYLOAD_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20

#: Proposal fields the dashboard is allowed to see. Everything else in the
#: audit record -- including anything an account snapshot might carry -- is
#: dropped rather than filtered later.
PROPOSAL_FIELDS = (
    "proposal_id",
    "symbol",
    "side",
    "quantity",
    "reference_price",
    "notional",
    "status",
    "risk_passed",
    "risk_failures",
    "score",
    "confidence",
    "regime",
    "trigger_reason",
    "system2_decision",
    "system2_confidence",
)


def build_payload(
    config: AgentConfig,
    *,
    audit: AuditLog | None = None,
    store: PriceStore | None = None,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    hurdle_pct: Decimal = DEFAULT_HURDLE_PCT,
    limit: int | None = None,
    repo_url: str | None = None,
) -> dict[str, Any]:
    """Assemble everything the dashboard renders, from local state only."""
    audit = audit or AuditLog(config.audit_path)
    store = store or PriceStore(config.price_store_path)

    candles_by_symbol = {
        symbol: store.candles(
            symbol, interval_minutes=config.strategy.bar_interval_minutes, limit=500
        )
        for symbol in store.symbols()
    }

    proposals: list[dict[str, Any]] = []
    outcomes: list[Outcome] = []

    records = list(audit.events(kind=KIND_PROPOSAL))
    if limit is not None and limit > 0:
        records = records[-limit:]

    for record in records:
        row = {field: record.get(field) for field in PROPOSAL_FIELDS}
        row["proposed_at"] = record.get("recorded_at")

        symbol = str(record.get("symbol", ""))
        outcome = outcome_from_proposal_record(
            record,
            candles_by_symbol.get(symbol, []),
            horizon_bars=horizon_bars,
            hurdle_pct=hurdle_pct,
        )
        if outcome is not None:
            outcomes.append(outcome)
            row["outcome"] = outcome.to_dict()
        else:
            row["outcome"] = None

        # Blocked, never-escalated and System-2-declined candidates are shown
        # for the record, but only a live proposal gets an Accept button.
        row["actionable"] = bool(record.get("risk_passed")) and (
            record.get("status", ProposalStatus.PROPOSED.value) == ProposalStatus.PROPOSED.value
        )
        proposals.append(row)

    proposals.sort(key=lambda r: str(r.get("proposed_at") or ""), reverse=True)

    activity = audit.daily_activity()

    return {
        "version": PAYLOAD_VERSION,
        "generated_at": utcnow().isoformat(),
        "repo_url": repo_url,
        "execution_mode": config.execution_mode.value,
        "watchlist": list(config.watchlist),
        "scoring": {
            "horizon_bars": horizon_bars,
            "hurdle_pct": str(hurdle_pct),
            "bar_interval_minutes": config.strategy.bar_interval_minutes,
        },
        "proposals": proposals,
        "stats": {
            "overall": aggregate(outcomes).to_dict(),
            "by_regime": {k: v.to_dict() for k, v in group_by(outcomes, "regime").items()},
            "by_symbol": {k: v.to_dict() for k, v in group_by(outcomes, "symbol").items()},
            "by_side": {k: v.to_dict() for k, v in group_by(outcomes, "side").items()},
            # The shadow run's question: did what System 2 proposed beat what
            # it passed on, and what the trigger held back?
            "by_status": {k: v.to_dict() for k, v in group_by(outcomes, "status").items()},
        },
        "today": {
            "executions": activity.execution_count,
            "executed_notional": str(round_money(activity.executed_notional)),
            "realized_pnl": str(round_money(activity.realized_pnl)),
            "proposals": activity.proposal_count,
        },
    }


def _request(
    url: str, *, token: str, method: str, body: dict[str, Any] | None = None, timeout: int
) -> Any:
    return net.request_json(
        method, url, headers={"Authorization": f"Bearer {token}"}, body=body, timeout=timeout
    )


def push(
    payload: dict[str, Any],
    *,
    base_url: str,
    token: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST the payload to the dashboard."""
    if not base_url.startswith("https://") and "localhost" not in base_url:
        # A bearer token over plaintext would be readable in transit.
        raise AgentError(
            f"refusing to send a bearer token to a non-HTTPS URL: {base_url}"
        )
    url = base_url.rstrip("/") + "/api/proposals"
    return _request(url, token=token, method="POST", body=payload, timeout=timeout) or {}


def fetch_decisions(
    *, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> list[Decision]:
    """GET accept/decline decisions recorded on the dashboard.

    Every row is parsed through :func:`decisions.decision_from_dict`, which
    validates the proposal id and redacts the risk-override phrase -- the
    dashboard is treated as untrusted input even though the operator owns it.
    """
    if not base_url.startswith("https://") and "localhost" not in base_url:
        raise AgentError(
            f"refusing to send a bearer token to a non-HTTPS URL: {base_url}"
        )
    url = base_url.rstrip("/") + "/api/decisions"
    response = _request(url, token=token, method="GET", timeout=timeout) or {}
    rows = response.get("decisions") if isinstance(response, dict) else response
    if not isinstance(rows, list):
        return []

    decisions: list[Decision] = []
    for row in rows:
        try:
            decisions.append(decision_from_dict(row))
        except AgentError:
            # One malformed row must not discard the rest.
            continue
    return decisions


@dataclass(frozen=True)
class SyncResult:
    pushed: int
    decisions: list[Decision]
    new_decisions: list[Decision]


def sync(
    config: AgentConfig,
    *,
    audit: AuditLog,
    store: PriceStore,
    base_url: str,
    token: str,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    hurdle_pct: Decimal = DEFAULT_HURDLE_PCT,
    limit: int | None = None,
    repo_url: str | None = None,
) -> SyncResult:
    """Push proposals up, pull decisions back, and note each *new* decision once.

    The dashboard returns every decision it holds on each call, so recording
    them all every time would append the same note on every sync -- which, from
    a scheduler, means every few minutes forever.
    """
    payload = build_payload(
        config,
        audit=audit,
        store=store,
        horizon_bars=horizon_bars,
        hurdle_pct=hurdle_pct,
        limit=limit,
        repo_url=repo_url,
    )
    push(payload, base_url=base_url, token=token)
    decisions = fetch_decisions(base_url=base_url, token=token)

    noted = {
        (str(n["decision"].get("proposal_id")), str(n["decision"].get("decided_at")))
        for n in audit.events(kind=KIND_NOTE)
        if isinstance(n.get("decision"), dict)
    }
    new: list[Decision] = []
    for decision in decisions:
        record = decision.to_dict()
        if (str(record.get("proposal_id")), str(record.get("decided_at"))) in noted:
            continue
        audit.append(
            KIND_NOTE,
            {
                "message": f"dashboard decision: {decision.kind.value} {decision.proposal_id}",
                "decision": record,
            },
        )
        new.append(decision)
    return SyncResult(pushed=len(payload["proposals"]), decisions=decisions, new_decisions=new)


def latest_decision_for(decisions: Sequence[Decision], proposal_id: str) -> Decision | None:
    """The most recent decision for a proposal, since a mind can be changed."""
    matching = [d for d in decisions if d.proposal_id == proposal_id]
    if not matching:
        return None
    return max(matching, key=lambda d: d.decided_at)
