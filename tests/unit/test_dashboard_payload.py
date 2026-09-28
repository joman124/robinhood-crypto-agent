"""What the dashboard is allowed to see, and what it must never receive."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
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
    ExecutionPlan,
    ExecutionRecord,
    OrderType,
    Proposal,
    ProposalStatus,
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
        reason="dip: closed 95.00, 5.0% under the anchor 100.00",
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
        derived = {
            "proposed_at", "outcome", "actionable", "signals", "notes", "plan",
            "risk_findings", "drift_pct", "execution",
        }
        assert set(payload["proposals"][0]) <= set(PROPOSAL_FIELDS) | derived

    def test_account_describing_risk_messages_are_withheld(self, config):
        audit = AuditLog(config.audit_path)
        p = proposal()
        p = replace(
            p,
            risk=RiskDecision(
                [
                    RiskFinding("spread", True, "bid/ask spread is 0.1% of mark"),
                    RiskFinding("concentration", False, "would be 40% of the $512.00 portfolio"),
                ]
            ),
        )
        audit.record_proposal(p)
        findings = build_payload(config, audit=audit)["proposals"][0]["risk_findings"]
        by_rule = {f["rule"]: f for f in findings}
        assert by_rule["spread"]["message"] == "bid/ask spread is 0.1% of mark"
        assert by_rule["concentration"] == {"rule": "concentration", "passed": False, "blocking": True}
        assert "$512" not in json.dumps(findings)

    def test_heartbeat_error_text_is_withheld(self, config):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.heartbeat_path.write_text(
            json.dumps(
                {
                    "last_cycle_at": utcnow().isoformat(),
                    "last_error": {"task": "quotes", "message": "401 key=abc", "at": "x"},
                }
            )
        )
        pipeline = build_payload(config, audit=AuditLog(config.audit_path))["pipeline"]
        assert pipeline["last_error"] == {"task": "quotes", "at": "x"}
        assert pipeline["sync_interval_seconds"] == config.pipeline.sync_interval_seconds
        assert "abc" not in json.dumps(pipeline)

    def test_executions_are_summarized_without_order_ids(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal())
        audit.record_execution(
            ExecutionRecord(
                proposal_id="abc123def456", symbol="BTC-USD", side=Side.BUY,
                recorded_at=utcnow(), requested_quantity=Decimal("0.001"),
                filled_quantity=Decimal("0.001"), notional=Decimal("0.10"),
                order_id="ORDER-SECRET", state="filled", ref_id="REF-SECRET",
            )
        )
        execution = build_payload(config, audit=audit)["proposals"][0]["execution"]
        assert execution["tranches"] == 1
        assert execution["filled_quantity"] == "0.001"
        assert "SECRET" not in json.dumps(execution)


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

    def test_drift_is_measured_against_the_last_seen_mark(self, config):
        audit = AuditLog(config.audit_path)
        store = PriceStore(config.price_store_path)
        store.import_candles(make_candles([101.0], anchor=utcnow() - timedelta(hours=1)))
        audit.record_proposal(proposal())  # reference price 100
        payload = build_payload(config, audit=audit, store=store)
        assert payload["proposals"][0]["drift_pct"] == "1.000"
        assert payload["limits"]["price_drift_tolerance_pct"] == "0.5"
        assert payload["kill_switch"]["engaged"] is False

    def test_newest_proposal_comes_first(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_proposal(proposal("aaa111aaa111"))
        audit.record_proposal(proposal("bbb222bbb222"))
        payload = build_payload(config, audit=audit)
        first, second = payload["proposals"][0], payload["proposals"][1]
        assert first["proposed_at"] >= second["proposed_at"]

    def test_a_ladder_proposal_shows_its_reason_and_is_not_hit_rate_scored(self, config):
        audit = AuditLog(config.audit_path)
        store = PriceStore(config.price_store_path)
        store.import_candles(make_candles([100] * 20, anchor=utcnow() - timedelta(hours=20)))
        audit.record_proposal(
            proposal(),
            {"strategy": "ladder", "rule": "dip", "step": 0, "trigger_reason": "dip: ..."},
        )
        [row] = build_payload(config, audit=audit, store=store)["proposals"]
        assert (row["strategy"], row["rule"], row["step"]) == ("ladder", "dip", 0)
        assert row["notes"] == ["dip: closed 95.00, 5.0% under the anchor 100.00"]
        assert row["signals"] == [] and row["score"] is None
        assert row["outcome"] is None

    def test_the_split_sends_realized_pnl_but_not_what_it_holds(self, config):
        audit = AuditLog(config.audit_path)
        for pid, side, rule in (("b0", "buy", "entry"), ("s0", "sell", "stop")):
            detail = {"sleeve": "short-term", "rule": rule, "day": "2026-05-01T00:00:00+00:00"}
            audit.append(
                KIND_PROPOSAL,
                {
                    "proposal_id": pid, "symbol": "BTC-USD", "side": side, "strategy": "split",
                    "notional": "0", "reference_price": "0",
                    "proposal": {"sizing_detail": {"split": detail}},
                },
            )
        fills = (("b0", Side.BUY, "0.2", "19.00"), ("s0", Side.SELL, "0.1", "11.00"))
        for pid, side, qty, notional in fills:
            audit.record_execution(
                ExecutionRecord(
                    proposal_id=pid, symbol="BTC-USD", side=side, recorded_at=utcnow(),
                    requested_quantity=Decimal(qty), filled_quantity=Decimal(qty),
                    notional=Decimal(notional), order_id=f"o-{pid}", state="filled",
                )
            )
        # 0.1 x (110 - 95) realized. The 0.1 still held, and its $9.50 cost, stay here.
        payload = build_payload(config, audit=audit)
        assert payload["split_realized_pnl"] == {"BTC-USD": "1.50"}
        assert "ladder_realized_pnl" not in payload

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


class TestSync:
    def test_each_decision_is_noted_once_however_often_it_syncs(self, config, monkeypatch):
        from robinhood_crypto_agent import dashboard

        decision = Decision("abc123def456", DecisionKind.ACCEPT, utcnow())
        monkeypatch.setattr(dashboard, "push", lambda payload, **kwargs: {})
        monkeypatch.setattr(dashboard, "fetch_decisions", lambda **kwargs: [decision])
        audit = AuditLog(config.audit_path)
        store = PriceStore(config.price_store_path)

        first = dashboard.sync(config, audit=audit, store=store, base_url="https://d", token="t")
        second = dashboard.sync(config, audit=audit, store=store, base_url="https://d", token="t")
        assert len(first.new_decisions) == 1 and second.new_decisions == []
        assert len(list(audit.events(kind="note"))) == 1

    def test_only_a_live_proposal_is_actionable(self, config):
        audit = AuditLog(config.audit_path)
        declined = proposal()
        audit.record_proposal(
            Proposal(**{**declined.__dict__, "status": ProposalStatus.DECLINED_BY_SYSTEM2}),
            {"system2_decision": "pass", "system2": {"rationale": "you hold 2 BTC"}},
        )
        [row] = build_payload(config, audit=audit)["proposals"]
        assert row["actionable"] is False
        assert row["system2_decision"] == "pass"
        assert "system2" not in row  # the rationale may quote holdings; it stays local
