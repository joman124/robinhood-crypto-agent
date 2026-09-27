"""Rehydrating proposals from the audit log.

``analyze`` and ``approve`` are separate commands, usually separate turns, and
possibly separate sessions -- so a proposal has to survive a round trip through
JSON. It is read back from the audit log rather than from a scratch file on
purpose: the thing a human approves is then literally the thing that was
recorded, with no second copy that could drift from it.
"""

from __future__ import annotations

from typing import Any

from .errors import AuditError
from .models import (
    CompositeView,
    Direction,
    ExecutionPlan,
    OrderType,
    Proposal,
    ProposalStatus,
    Regime,
    RiskDecision,
    RiskFinding,
    Side,
    Signal,
    Tranche,
    parse_timestamp,
)
from .numeric import to_decimal


def proposal_from_dict(data: dict[str, Any]) -> Proposal:
    """Reconstruct a :class:`Proposal` from its serialized form."""
    try:
        view = _view_from_dict(data["view"])
        plan = _plan_from_dict(data["plan"])
        risk = _risk_from_dict(data["risk"])
        return Proposal(
            proposal_id=str(data["proposal_id"]),
            symbol=str(data["symbol"]),
            side=Side(str(data["side"])),
            quantity=to_decimal(data["quantity"], field="quantity"),
            reference_price=to_decimal(data["reference_price"], field="reference_price"),
            notional=to_decimal(data["notional"], field="notional"),
            created_at=parse_timestamp(str(data["created_at"])),
            view=view,
            plan=plan,
            risk=risk,
            status=ProposalStatus(str(data.get("status", ProposalStatus.PROPOSED.value))),
            sizing_detail=dict(data.get("sizing_detail") or {}),
            spread_pct=(
                to_decimal(data["spread_pct"], field="spread_pct")
                if data.get("spread_pct") is not None
                else None
            ),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AuditError(f"could not rehydrate proposal: {exc}") from exc


def _view_from_dict(data: dict[str, Any]) -> CompositeView:
    return CompositeView(
        symbol=str(data["symbol"]),
        regime=Regime(str(data.get("regime", Regime.UNKNOWN.value))),
        score=float(data.get("score", 0.0)),
        confidence=float(data.get("confidence", 0.0)),
        direction=Direction(str(data.get("direction", Direction.FLAT.value))),
        signals=[_signal_from_dict(s) for s in data.get("signals") or []],
        weights={str(k): float(v) for k, v in (data.get("weights") or {}).items()},
        notes=[str(n) for n in data.get("notes") or []],
    )


def _signal_from_dict(data: dict[str, Any]) -> Signal:
    return Signal(
        name=str(data["name"]),
        symbol=str(data["symbol"]),
        score=float(data["score"]),
        confidence=float(data["confidence"]),
        direction=Direction(str(data["direction"])),
        rationale=str(data.get("rationale", "")),
        detail=dict(data.get("detail") or {}),
    )


def _plan_from_dict(data: dict[str, Any]) -> ExecutionPlan:
    return ExecutionPlan(
        style=str(data.get("style", "PROMPT")),
        tranches=[
            Tranche(
                index=int(t["index"]),
                quantity=to_decimal(t["quantity"], field="tranche.quantity"),
                target_price=to_decimal(t["target_price"], field="tranche.target_price"),
                order_type=OrderType(str(t["order_type"])),
            )
            for t in data.get("tranches") or []
        ],
        rationale=str(data.get("rationale", "")),
    )


def _risk_from_dict(data: dict[str, Any]) -> RiskDecision:
    return RiskDecision(
        findings=[
            RiskFinding(
                rule=str(f["rule"]),
                passed=bool(f["passed"]),
                message=str(f.get("message", "")),
                blocking=bool(f.get("blocking", True)),
            )
            for f in data.get("findings") or []
        ]
    )
