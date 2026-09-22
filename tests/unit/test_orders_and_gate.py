"""Execution plans, order payload construction, and the approval gate."""

from dataclasses import replace
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import AuditLog
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.errors import ApprovalError, ContractViolation, KillSwitchEngaged
from robinhood_crypto_agent.execution.gate import OVERRIDE_PHRASE, ApprovalGate
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.execution.orders import (
    STYLE_PROMPT,
    STYLE_STAGED,
    build_order_request,
    build_plan_requests,
    plan_for_view,
)
from robinhood_crypto_agent.mcp.contract import CRYPTO_TOOLS, validate_crypto_order_args
from robinhood_crypto_agent.models import (
    CompositeView,
    Direction,
    ExecutionMode,
    ExecutionPlan,
    ExecutionRecord,
    OrderType,
    PairConstraints,
    Proposal,
    ProposalStatus,
    Quote,
    Regime,
    RiskDecision,
    RiskFinding,
    Side,
    Tranche,
    utcnow,
)
from robinhood_crypto_agent.sizing import SizingResult

PAIR = PairConstraints(
    "BTC-USD",
    Decimal("0.00000001"),
    price_increment=Decimal("0.01"),
    min_order_size=Decimal("0.000001"),
)


def sizing(quantity="0.003", side=Side.BUY, price="80000"):
    return SizingResult(
        symbol="BTC-USD",
        side=side,
        quantity=Decimal(quantity),
        notional=Decimal(quantity) * Decimal(price),
        reference_price=Decimal(price),
    )


class TestPlans:
    def test_trending_regime_fills_promptly(self):
        plan = plan_for_view(sizing(), regime=Regime.TRENDING, constraints=PAIR)
        assert plan.style == STYLE_PROMPT
        assert len(plan.tranches) == 1

    def test_ranging_regime_ladders_limits(self):
        plan = plan_for_view(sizing(), regime=Regime.RANGING, constraints=PAIR, atr=500.0)
        assert plan.style == STYLE_STAGED
        assert len(plan.tranches) == 3
        prices = [t.target_price for t in plan.tranches]
        assert prices == sorted(prices, reverse=True)  # a buy ladders downward

    def test_a_sell_ladder_goes_the_other_way(self):
        plan = plan_for_view(
            sizing(side=Side.SELL), regime=Regime.RANGING, constraints=PAIR, atr=500.0
        )
        prices = [t.target_price for t in plan.tranches]
        assert prices == sorted(prices)

    def test_tranches_sum_to_exactly_the_approved_quantity(self):
        """Rounding must never let the plan exceed what risk approved."""
        for quantity in ("0.003", "0.00000007", "1.23456789"):
            plan = plan_for_view(
                sizing(quantity=quantity), regime=Regime.RANGING, constraints=PAIR, atr=100.0
            )
            assert plan.total_quantity == Decimal(quantity)

    def test_market_only_pair_forces_a_single_market_order(self):
        constraints = PairConstraints("X-USD", Decimal("0.1"), market_orders_only=True)
        plan = plan_for_view(sizing(), regime=Regime.RANGING, constraints=constraints)
        assert plan.style == STYLE_PROMPT
        assert plan.tranches[0].order_type is OrderType.MARKET

    def test_limit_prices_snap_conservatively_to_the_tick(self):
        """A buy limit rounds down, so snapping never makes it more aggressive."""
        coarse = PairConstraints("BTC-USD", Decimal("0.00000001"), price_increment=Decimal("100"))
        plan = plan_for_view(
            sizing(price="80050"), regime=Regime.RANGING, constraints=coarse, atr=10.0
        )
        assert plan.tranches[0].target_price <= Decimal("80050")
        assert plan.tranches[0].target_price % Decimal("100") == 0


def make_proposal(*, passed=True, plan=None, quantity="0.003"):
    findings = [RiskFinding("test", passed, "because")]
    return Proposal(
        proposal_id="abc123def456",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal(quantity),
        reference_price=Decimal("80000"),
        notional=Decimal("240"),
        created_at=utcnow(),
        view=CompositeView("BTC-USD", Regime.TRENDING, 0.6, 0.8, Direction.LONG, [], {}),
        plan=plan
        or ExecutionPlan(
            STYLE_PROMPT,
            [Tranche(0, Decimal(quantity), Decimal("80000"), OrderType.LIMIT)],
            "prompt",
        ),
        risk=RiskDecision(findings),
        status=ProposalStatus.PROPOSED if passed else ProposalStatus.REJECTED_BY_RISK,
    )


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",), rhs_account_number="123456789", data_dir=tmp_path
    )


class TestOrderRequests:
    def test_payload_satisfies_the_contract(self, config):
        request = build_order_request(make_proposal(), config=config)
        assert request.tool == CRYPTO_TOOLS["preview"]
        assert validate_crypto_order_args(request.arguments)
        assert request.arguments["rhs_account_number"] == "123456789"
        assert "ref_id" not in request.arguments  # previews take no idempotency key

    def test_place_payload_carries_a_ref_id(self, config):
        request = build_order_request(
            make_proposal(), config=config, tool=CRYPTO_TOOLS["place"]
        )
        assert validate_crypto_order_args(request.arguments)
        import uuid

        uuid.UUID(request.arguments["ref_id"])

    def test_supplied_ref_id_is_reused_for_retries(self, config):
        ref = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        request = build_order_request(
            make_proposal(), config=config, tool=CRYPTO_TOOLS["place"], ref_id=ref
        )
        assert request.arguments["ref_id"] == ref

    def test_missing_account_number_is_refused_with_guidance(self, tmp_path):
        config = AgentConfig(watchlist=("BTC-USD",), rhs_account_number=None, data_dir=tmp_path)
        with pytest.raises(ContractViolation, match="not the alphanumeric"):
            build_order_request(make_proposal(), config=config)

    def test_unknown_tranche_is_refused(self, config):
        with pytest.raises(ContractViolation, match="no tranche 7"):
            build_order_request(make_proposal(), config=config, tranche_index=7)

    def test_every_tranche_of_a_staged_plan_validates(self, config):
        plan = plan_for_view(sizing(), regime=Regime.RANGING, constraints=PAIR, atr=500.0)
        requests = build_plan_requests(make_proposal(plan=plan), config=config)
        assert len(requests) == 3
        for request in requests:
            assert validate_crypto_order_args(request.arguments)


class TestApprovalGate:
    def gate(self, config):
        return ApprovalGate(
            config,
            kill_switch=KillSwitch(config.kill_switch_path),
            audit=AuditLog(config.audit_path),
        )

    def live(self, mark="80000", symbol="BTC-USD"):
        return Quote(symbol, Decimal(mark), Decimal(mark), Decimal(mark), utcnow())

    def test_a_named_approval_authorizes(self, config):
        auth = self.gate(config).authorize(
            make_proposal(),
            approval_text="execute abc123def456",
            live_quote=self.live(),
        )
        assert auth.proposal_id == "abc123def456"
        assert not auth.overridden
        assert validate_crypto_order_args(auth.request.arguments)

    @pytest.mark.parametrize(
        "status", [ProposalStatus.NOT_ESCALATED, ProposalStatus.DECLINED_BY_SYSTEM2]
    )
    def test_a_candidate_rhca_run_did_not_propose_cannot_be_approved(self, config, status):
        """Held back or passed on is a record, not a proposal -- even with a named id."""
        held = replace(make_proposal(), status=status)
        with pytest.raises(ApprovalError, match=status.value):
            self.gate(config).authorize(
                held, approval_text="execute abc123def456", live_quote=self.live()
            )

    @pytest.mark.parametrize(
        "text", ["that looks good", "yes", "go ahead", "approve the btc one", ""]
    )
    def test_vague_approval_is_refused(self, config, text):
        with pytest.raises(ApprovalError):
            self.gate(config).authorize(
                make_proposal(), approval_text=text, live_quote=self.live()
            )

    def test_another_proposals_id_does_not_authorize_this_one(self, config):
        with pytest.raises(ApprovalError, match="does not name proposal"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute 999888777666",
                live_quote=self.live(),
            )

    def test_id_must_match_on_a_word_boundary(self, config):
        with pytest.raises(ApprovalError, match="does not name proposal"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute xxabc123def456xx",
                live_quote=self.live(),
            )

    def test_blocked_proposal_needs_the_override_phrase(self, config):
        with pytest.raises(ApprovalError, match=OVERRIDE_PHRASE):
            self.gate(config).authorize(
                make_proposal(passed=False),
                approval_text="execute abc123def456",
                live_quote=self.live(),
            )

    def test_override_phrase_authorizes_and_is_flagged(self, config):
        auth = self.gate(config).authorize(
            make_proposal(passed=False),
            approval_text=f"execute abc123def456 {OVERRIDE_PHRASE}",
            live_quote=self.live(),
        )
        assert auth.overridden
        assert "RISK OVERRIDE" in auth.describe()

    def test_override_on_a_passing_proposal_is_refused(self, config):
        """So the phrase cannot become a habitual incantation."""
        with pytest.raises(ApprovalError, match="does not apply"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text=f"execute abc123def456 {OVERRIDE_PHRASE}",
                live_quote=self.live(),
            )

    def test_price_drift_beyond_tolerance_is_refused(self, config):
        with pytest.raises(ApprovalError, match="price has moved"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(mark="82000"),
            )

    def test_drift_within_tolerance_is_allowed(self, config):
        auth = self.gate(config).authorize(
            make_proposal(),
            approval_text="execute abc123def456",
            live_quote=self.live(mark="80200"),
        )
        assert auth.drift_pct == Decimal("0.25")

    def test_a_quote_for_the_wrong_symbol_is_refused(self, config):
        with pytest.raises(ApprovalError, match="but the proposal is for"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(symbol="ETH-USD"),
            )

    def test_kill_switch_blocks_authorization(self, config):
        KillSwitch(config.kill_switch_path).engage("manual")
        with pytest.raises(KillSwitchEngaged):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(),
            )

    def test_auto_mode_is_refused(self, tmp_path):
        config = AgentConfig(
            execution_mode=ExecutionMode.AUTO,
            watchlist=("BTC-USD",),
            rhs_account_number="1",
            data_dir=tmp_path,
        )
        with pytest.raises(ApprovalError, match="only implements 'propose_only'"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(),
            )

    def test_a_filled_proposal_cannot_be_re_approved(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_execution(
            ExecutionRecord(
                proposal_id="abc123def456",
                symbol="BTC-USD",
                side=Side.BUY,
                recorded_at=utcnow(),
                requested_quantity=Decimal("0.003"),
                filled_quantity=Decimal("0.003"),
                notional=Decimal("240"),
                order_id="o",
                state="filled",
            )
        )
        with pytest.raises(ApprovalError, match="already filled"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(),
            )

    def test_a_tranche_larger_than_the_remainder_is_refused(self, config):
        audit = AuditLog(config.audit_path)
        audit.record_execution(
            ExecutionRecord(
                proposal_id="abc123def456",
                symbol="BTC-USD",
                side=Side.BUY,
                recorded_at=utcnow(),
                requested_quantity=Decimal("0.002"),
                filled_quantity=Decimal("0.002"),
                notional=Decimal("160"),
                order_id="o",
                state="filled",
            )
        )
        with pytest.raises(ApprovalError, match="only 0.001 remains"):
            self.gate(config).authorize(
                make_proposal(),
                approval_text="execute abc123def456",
                live_quote=self.live(),
            )
