"""A proposal must survive the round trip through the audit log."""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import AuditLog
from robinhood_crypto_agent.errors import AuditError
from robinhood_crypto_agent.models import (
    ExecutionPlan,
    OrderType,
    Proposal,
    ProposalStatus,
    RiskDecision,
    RiskFinding,
    Side,
    Tranche,
    utcnow,
)
from robinhood_crypto_agent.serde import proposal_from_dict


def proposal():
    return Proposal(
        proposal_id="abc123",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal("0.00000123"),
        reference_price=Decimal("80123.45"),
        notional=Decimal("0.10"),
        created_at=utcnow(),
        reason="dip: closed 76,000.00, 5.0% under the anchor 80,000.00",
        plan=ExecutionPlan(
            "STAGED",
            [
                Tranche(0, Decimal("0.000001"), Decimal("80000"), OrderType.LIMIT),
                Tranche(1, Decimal("0.00000023"), Decimal("79500"), OrderType.LIMIT),
            ],
            "ladder",
        ),
        risk=RiskDecision([RiskFinding("spread", False, "too wide", blocking=False)]),
        status=ProposalStatus.PROPOSED,
        sizing_detail={"ladder": {"rule": "dip", "step": 0, "anchor": "80000"}},
    )


def test_round_trip_preserves_every_field():
    original = proposal()
    restored = proposal_from_dict(original.to_dict())
    assert restored == original


def test_decimals_survive_without_float_drift():
    """Serializing through float would corrupt an 8dp quantity."""
    restored = proposal_from_dict(proposal().to_dict())
    assert restored.quantity == Decimal("0.00000123")
    assert restored.plan.total_quantity == Decimal("0.00000123")


def test_round_trip_through_the_audit_log(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    original = proposal()
    log.record_proposal(original)
    record = log.find_proposal("abc123")
    assert proposal_from_dict(record["proposal"]) == original


def test_non_blocking_findings_keep_their_flag():
    restored = proposal_from_dict(proposal().to_dict())
    assert restored.risk.passed  # the only failure is non-blocking
    assert restored.risk.warnings


def test_corrupt_payload_is_reported_clearly():
    with pytest.raises(AuditError, match="could not rehydrate"):
        proposal_from_dict({"proposal_id": "x"})


def test_a_system_1_proposal_still_reads():
    """Its records in the audit log carry a composite view, not a reason."""
    legacy = proposal().to_dict()
    del legacy["reason"]
    legacy["view"] = {
        "symbol": "BTC-USD",
        "regime": "trending",
        "score": 0.42,
        "confidence": 0.77,
        "direction": "long",
        "signals": [],
        "weights": {},
        "notes": ["a note"],
    }
    restored = proposal_from_dict(legacy)
    assert restored.reason == (
        "System 1: long, score +0.420, confidence 0.770, regime trending; a note"
    )
