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
Account numbers, buying power, portfolio value, position sizes and order ids. The dashboard's job is to show what the agent *suggested* and whether
those suggestions were any good. It does not need to know how much money is
behind them, and a dashboard that never receives that data cannot leak it.

The split's realized P&L per symbol is sent, as today's realized P&L already
is: it is how the split is measured. What it holds, and what that cost, is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from . import net
from .audit import KIND_EXECUTION, KIND_NOTE, KIND_PROPOSAL, AuditLog
from .config import AgentConfig
from .decisions import Decision, decision_from_dict
from .errors import AgentError
from .execution.kill_switch import KillSwitch
from .ledger import split_book
from .models import ProposalStatus, utcnow
from .numeric import abs_pct_drift, format_decimal, round_money, to_decimal
from .outcomes import (
    DEFAULT_HORIZON_BARS,
    DEFAULT_HURDLE_PCT,
    Outcome,
    aggregate,
    group_by,
    outcome_from_proposal_record,
)
from .store import PriceStore
from .symbols import canonical

PAYLOAD_VERSION = 2
DEFAULT_TIMEOUT_SECONDS = 20

#: Risk rules whose message says nothing about the account. The rest quote the
#: portfolio value, holdings, today's traded dollars or realized loss, so the
#: dashboard gets their pass/fail verdict and never their text.
SHAREABLE_FINDING_MESSAGES = frozenset(
    {
        "execution_mode",
        "kill_switch",
        "watchlist",
        "pair_tradable",
        "order_type_supported",
        "quote_freshness",
        "spread",
        # System 1's, retired; its records in the log still carry them.
        "signal_confidence",
        "signal_strength",
    }
)

MAX_TEXT = 300

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
    "strategy",
    "sleeve",
    "rule",
    "step",
    "trigger_reason",
    # System 1's, retired 2026-09-27. Absent on the ladder's and the split's
    # proposals; its own records in the log still carry them.
    "score",
    "confidence",
    "regime",
    "system2_decision",
    "system2_confidence",
)


def _split_realized_pnl(config: AgentConfig, audit: AuditLog) -> dict[str, str]:
    """Realized P&L per coin, both sleeves together, from the split's recorded fills."""
    book = split_book(
        audit,
        long_capital=config.strategy.split_long_capital,
        short_capital=config.strategy.split_short_capital,
    )
    totals: dict[str, Decimal] = {}
    for sleeve in (book.long, book.short):
        for symbol, holding in sleeve.holdings.items():
            if holding.realized_pnl or holding.last_fill_at is not None:
                totals[symbol] = totals.get(symbol, Decimal(0)) + holding.realized_pnl
    return {symbol: str(round_money(total)) for symbol, total in sorted(totals.items())}


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
    marks = _latest_marks(store, config.watchlist)
    executions = _executions_by_proposal(audit)

    records = list(audit.events(kind=KIND_PROPOSAL))
    if limit is not None and limit > 0:
        records = records[-limit:]

    for record in records:
        row = {field: record.get(field) for field in PROPOSAL_FIELDS}
        row["proposed_at"] = record.get("recorded_at")
        row.update(_proposal_detail(record))
        row["drift_pct"] = _drift(
            record.get("reference_price"), marks.get(canonical(str(record.get("symbol", ""))))
        )
        row["execution"] = executions.get(str(record.get("proposal_id")))

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

        # Blocked proposals (and System 1's never-escalated and declined
        # candidates) are shown for the record, but only a live proposal gets
        # an Accept button.
        row["actionable"] = bool(record.get("risk_passed")) and (
            record.get("status", ProposalStatus.PROPOSED.value) == ProposalStatus.PROPOSED.value
        )
        proposals.append(row)

    proposals.sort(key=lambda r: str(r.get("proposed_at") or ""), reverse=True)

    activity = audit.daily_activity()
    kill = KillSwitch(config.kill_switch_path).state()

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
            "by_status": {k: v.to_dict() for k, v in group_by(outcomes, "status").items()},
        },
        "today": {
            "executions": activity.execution_count,
            "executed_notional": str(round_money(activity.executed_notional)),
            "realized_pnl": str(round_money(activity.realized_pnl)),
            "proposals": activity.proposal_count,
        },
        "split_realized_pnl": _split_realized_pnl(config, audit),
        "limits": {
            "price_drift_tolerance_pct": str(config.risk.price_drift_tolerance_pct),
        },
        "market": {
            symbol: {"mark": format_decimal(mark), "observed_at": at}
            for symbol, (mark, at) in marks.items()
        },
        "kill_switch": {
            "engaged": kill.engaged,
            "reason": kill.reason,
            "engaged_at": kill.engaged_at.isoformat() if kill.engaged_at else None,
        },
        "pipeline": _pipeline(config),
    }


def _proposal_detail(record: dict[str, Any]) -> dict[str, Any]:
    """The reason, plan and risk verdicts from the stored proposal, minus
    anything that describes the account. System 1's records carry signals."""
    stored = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    view = stored.get("view") if isinstance(stored.get("view"), dict) else {}
    plan = stored.get("plan") if isinstance(stored.get("plan"), dict) else {}
    risk = stored.get("risk") if isinstance(stored.get("risk"), dict) else {}
    weights = view.get("weights") if isinstance(view.get("weights"), dict) else {}

    signals = [
        {
            "name": str(s.get("name", "")),
            "score": s.get("score"),
            "confidence": s.get("confidence"),
            "direction": s.get("direction"),
            "weight": weights.get(s.get("name")),
            "rationale": str(s.get("rationale", ""))[:MAX_TEXT],
        }
        for s in view.get("signals") or []
        if isinstance(s, dict)
    ]
    findings = []
    for f in risk.get("findings") or []:
        if not isinstance(f, dict):
            continue
        rule = str(f.get("rule", ""))
        finding = {
            "rule": rule,
            "passed": bool(f.get("passed")),
            "blocking": bool(f.get("blocking", True)),
        }
        if rule in SHAREABLE_FINDING_MESSAGES:
            finding["message"] = str(f.get("message", ""))[:MAX_TEXT]
        findings.append(finding)

    notes = [str(n) for n in view.get("notes") or []]
    if stored.get("reason"):
        notes = [str(stored["reason"])]
    return {
        "signals": signals,
        "notes": [n[:MAX_TEXT] for n in notes],
        "plan": {
            "style": plan.get("style"),
            "tranches": len(plan.get("tranches") or []),
            "rationale": str(plan.get("rationale", ""))[:MAX_TEXT],
        }
        if plan
        else None,
        "risk_findings": findings,
    }


def _latest_marks(store: PriceStore, symbols: Sequence[str]) -> dict[str, tuple[Decimal, str]]:
    marks = {}
    for symbol in symbols:
        quote = store.latest_quote(symbol)
        if quote is not None:
            marks[canonical(symbol)] = (quote.mark, quote.observed_at.isoformat())
    return marks


def _drift(reference: Any, latest: tuple[Decimal, str] | None) -> str | None:
    """How far the last seen mark sits from the proposal's reference price --
    the same measure the approval gate refuses on, as of the last sync."""
    if latest is None or reference is None:
        return None
    try:
        ref = to_decimal(reference, field="reference_price")
    except AgentError:
        return None
    if ref <= 0:
        return None
    return str(round_money(abs_pct_drift(latest[0], ref), 3))


def _executions_by_proposal(audit: AuditLog) -> dict[str, dict[str, Any]]:
    """Per proposal: tranches logged, quantity filled, last state. No order ids."""
    summary: dict[str, dict[str, Any]] = {}
    for record in audit.events(kind=KIND_EXECUTION):
        pid = str(record.get("proposal_id", ""))
        entry = summary.setdefault(
            pid, {"tranches": 0, "filled_quantity": Decimal(0), "state": None, "overridden": False}
        )
        entry["tranches"] += 1
        entry["state"] = record.get("state")
        entry["overridden"] = entry["overridden"] or bool(record.get("overridden"))
        if str(record.get("state", "")).lower() not in {"rejected", "failed", "voided"}:
            try:
                entry["filled_quantity"] += to_decimal(record.get("filled_quantity", 0))
            except AgentError:
                pass
    return {
        pid: {**entry, "filled_quantity": format_decimal(entry["filled_quantity"])}
        for pid, entry in summary.items()
    }


def _pipeline(config: AgentConfig) -> dict[str, Any] | None:
    """The shadow loop's heartbeat. The last error's text is withheld: an API
    error can echo back whatever the request carried."""
    from .files import read_json
    from .runner import stale_after_seconds  # runner imports this module

    beat = read_json(config.heartbeat_path)
    if beat is None:
        return None
    error = beat.get("last_error") if isinstance(beat.get("last_error"), dict) else None
    return {
        "mode": beat.get("mode"),
        "started_at": beat.get("started_at"),
        "last_cycle_at": beat.get("last_cycle_at"),
        "cycles": beat.get("cycles"),
        "counts": beat.get("counts") or {},
        "services": beat.get("services") or {},
        "last_error": {"task": error.get("task"), "at": error.get("at")} if error else None,
        "stale_after_seconds": stale_after_seconds(config),
        # The dashboard sees the heartbeat only as often as the loop syncs.
        "sync_interval_seconds": config.pipeline.sync_interval_seconds,
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
