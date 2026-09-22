"""The trigger between System 1 and System 2: only strong, allowed, fresh ideas."""

from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.config import PipelineConfig
from robinhood_crypto_agent.models import (
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
    Tranche,
    utcnow,
)
from robinhood_crypto_agent.trigger import EscalationTrigger


def candidate(*, confidence=0.8, score=0.6, passed=True):
    return Proposal(
        proposal_id="abc123def456",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal("0.001"),
        reference_price=Decimal("80000"),
        notional=Decimal("80"),
        created_at=utcnow(),
        view=CompositeView("BTC-USD", Regime.TRENDING, score, confidence, Direction.LONG, [], {}),
        plan=ExecutionPlan(
            "PROMPT", [Tranche(0, Decimal("0.001"), Decimal("80000"), OrderType.LIMIT)], "now"
        ),
        risk=RiskDecision([RiskFinding("spread", passed, "because")]),
        status=ProposalStatus.PROPOSED,
    )


@pytest.fixture
def trigger():
    return EscalationTrigger(PipelineConfig())  # confidence 0.5, |score| 0.3, 60m, 24/day


def check(trigger, proposal, *, today=0, last=None):
    return trigger.evaluate(proposal, escalations_today=today, last_escalated_at=last, now=utcnow())


def test_a_strong_allowed_candidate_is_escalated(trigger):
    assert check(trigger, candidate()).escalate


def test_what_risk_blocked_is_never_escalated(trigger):
    """System 2 is never asked about a trade it could only be talked into."""
    verdict = check(trigger, candidate(confidence=1.0, score=1.0, passed=False))
    assert not verdict.escalate
    assert "spread" in verdict.reason


@pytest.mark.parametrize(
    "confidence, score, escalate",
    [(0.49, 0.9, False), (0.5, 0.9, True), (0.9, 0.29, False), (0.9, -0.6, True)],
)
def test_thresholds_apply_to_confidence_and_absolute_score(trigger, confidence, score, escalate):
    assert check(trigger, candidate(confidence=confidence, score=score)).escalate is escalate


def test_a_symbol_in_cooldown_waits(trigger):
    recent = check(trigger, candidate(), last=utcnow() - timedelta(minutes=10))
    assert not recent.escalate and "cooldown" in recent.reason
    assert check(trigger, candidate(), last=utcnow() - timedelta(minutes=61)).escalate


def test_the_daily_cap_is_also_the_spend_cap(trigger):
    verdict = check(trigger, candidate(), today=24)
    assert not verdict.escalate and "cap" in verdict.reason
