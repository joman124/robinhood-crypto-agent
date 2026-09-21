"""What the dashboard is allowed to see, and what it must never receive."""

from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import AuditLog
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.dashboard import (
    PROPOSAL_FIELDS,
    build_payload,
    fetch_decisions,
    latest_decision_for,
    push,
)
from robinhood_crypto_agent.decisions import Decision, DecisionKind
from robinhood_crypto_agent.errors import AgentError
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
from robinhood_crypto_agent.store import PriceStore
from tests.conftest import make_candles


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",), rhs_account_number="123456789", data_dir=tmp_path / "data"
    )


def proposal(proposal_id="abc123def456", *, passed=True, when=None):
    created = when or utcnow()
    return Proposal(
        proposal_id=proposal_id,
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal("0.001"),
        reference_price=Decimal("100"),
        notional=Decimal("0.10"),
        created_at=created,
        view=CompositeView("BTC-USD", Regime.TRENDING, 0.6, 0.8, Direction.LONG, [], {}),
        plan=ExecutionPlan(
            "PROMPT", [Tranche(0, Decimal("0.001"), Decimal("100"), OrderType.LIMIT)], "now"
        ),
        risk=RiskDecision([RiskFinding("spread", passed, "because")]),
        status=ProposalStatus.PROPOSED if passed else ProposalStatus.REJECTED_BY_RISK,
    )


class TestPayloadPrivacy:
    """Data the dashboard never receives cannot leak from it."""

    @pytest.mark.parametrize(
        "field",
        [
            "rhs_account_number",
            "account_number",
            "buying_power",
            "crypto_buying_power",
            "portfolio_value",
            "order_id",
            "ref_id",
            "raw_response",
        ],
    )
    def test_sensitive_fields_are_not_in_the_allowlist(self, field):
        assert field not in PROPOSAL_FIELDS

    def test_payload_rows_carry_only_allowlisted_keys(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal())
        payload = build_payload(config, audit=audit, store=PriceStore(config.price_store_path))
        allowed = set(PROPOSAL_FIELDS) | {"proposed_at", "outcome", "actionable"}
        assert set(payload["proposals"][0]) <= allowed


class TestPayloadShape:
    def test_empty_state_is_valid(self, config):
        payload = build_payload(config, audit=AuditLog(config.audit_path))
        assert payload["proposals"] == []
        assert payload["stats"]["overall"]["win_rate"] is None

    def test_blocked_proposals_are_included_but_not_actionable(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal("aaa111bbb222", passed=False))
        payload = build_payload(config, audit=audit)
        row = payload["proposals"][0]
        assert row["risk_passed"] is False
        assert row["actionable"] is False

    def test_passing_proposals_are_actionable(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal())
        payload = build_payload(config, audit=audit)
        assert payload["proposals"][0]["actionable"] is True

    def test_outcomes_are_attached_when_history_allows(self, config):
        audit = AuditLog(config.audit_path)
        store = PriceStore(config.price_store_path)
        anchor = utcnow() - timedelta(hours=20)
        store.import_candles(make_candles([100 + i * 2 for i in range(20)], anchor=anchor))

        audit.record_proposal(proposal())
        # Backdate the record so the horizon has elapsed.
        rows = audit.path.read_text().splitlines()
        import json

        parsed = [json.loads(r) for r in rows if r.strip()]
        for row in parsed:
            row["recorded_at"] = anchor.isoformat()
        audit.path.write_text("\n".join(json.dumps(r) for r in parsed) + "\n")

        payload = build_payload(config, audit=audit, store=store)
        assert payload["proposals"][0]["outcome"]["verdict"] == "win"
        assert payload["stats"]["overall"]["wins"] == 1

    def test_newest_proposal_comes_first(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal("aaa111aaa111"))
        audit.record_proposal(proposal("bbb222bbb222"))
        payload = build_payload(config, audit=audit)
        first, second = payload["proposals"][0], payload["proposals"][1]
        assert first["proposed_at"] >= second["proposed_at"]

    def test_limit_keeps_the_most_recent(self, config):
        audit = AuditLog(config.audit_path)
        for i in range(5):
            audit.record_proposal(proposal(f"{i:012x}"))
        payload = build_payload(config, audit=audit, limit=2)
        assert len(payload["proposals"]) == 2


class TestTransportSafety:
    @pytest.mark.parametrize("url", ["http://example.com", "http://1.2.3.4:8080"])
    def test_refuses_to_send_a_token_over_plaintext(self, url):
        with pytest.raises(AgentError, match="non-HTTPS"):
            push({}, base_url=url, token="secret")
        with pytest.raises(AgentError, match="non-HTTPS"):
            fetch_decisions(base_url=url, token="secret")

    def test_localhost_is_allowed_for_development(self):
        """Only the URL check is exercised; no request is made here."""
        with pytest.raises(AgentError) as excinfo:
            push({}, base_url="http://localhost:3000", token="secret")
        assert "non-HTTPS" not in str(excinfo.value)


def test_latest_decision_wins():
    """A mind can be changed; the later decision is the live one."""
    early = Decision("abc123def456", DecisionKind.ACCEPT, utcnow() - timedelta(hours=1))
    late = Decision("abc123def456", DecisionKind.DECLINE, utcnow())
    assert latest_decision_for([early, late], "abc123def456") is late
    assert latest_decision_for([early, late], "ffffffffffff") is None
