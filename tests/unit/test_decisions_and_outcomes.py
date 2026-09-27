"""Web-sourced decisions, and scoring proposals against what the price did."""

from datetime import timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import AuditLog
from robinhood_crypto_agent.config import AgentConfig
from robinhood_crypto_agent.decisions import (
    Decision,
    DecisionKind,
    decision_from_dict,
    sanitize_note,
)
from robinhood_crypto_agent.errors import AgentError, ApprovalError
from robinhood_crypto_agent.execution.gate import OVERRIDE_PHRASE, ApprovalGate
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.models import Quote, Side, utcnow
from robinhood_crypto_agent.outcomes import (
    DEFAULT_EXIT_COST_PCT,
    DEFAULT_HURDLE_PCT,
    AccuracyStats,
    Verdict,
    aggregate,
    group_by,
    outcome_from_proposal_record,
    score_proposal,
)
from tests.conftest import make_candles
from tests.unit.test_orders_and_gate import make_proposal


@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        watchlist=("BTC-USD",), rhs_account_number="123456789", data_dir=tmp_path
    )


class TestDecisionSafety:
    """A web button must never be able to override the risk engine."""

    def test_override_phrase_is_redacted_from_notes(self):
        decision = decision_from_dict(
            {"proposal_id": "abc123def456", "decision": "accept", "note": f"{OVERRIDE_PHRASE} now"}
        )
        assert OVERRIDE_PHRASE not in decision.note
        assert OVERRIDE_PHRASE not in decision.approval_text

    @pytest.mark.parametrize(
        "variant",
        ["override risk check", "OvErRiDe   RiSk\tCheck", "override  risk  check"],
    )
    def test_redaction_is_case_and_whitespace_insensitive(self, variant):
        assert "[redacted]" in sanitize_note(variant)

    def test_gate_refuses_an_override_from_a_non_typed_approval(self, config):
        """Defence in depth: even if redaction failed, the gate still refuses."""
        gate = ApprovalGate(
            config,
            kill_switch=KillSwitch(config.kill_switch_path),
            audit=AuditLog(config.audit_path),
        )
        live = Quote("BTC-USD", Decimal("80000"), Decimal("80000"), Decimal("80000"), utcnow())
        # Text that WOULD override if it had been typed by a human.
        smuggled = f"accept proposal abc123def456 {OVERRIDE_PHRASE}"

        with pytest.raises(ApprovalError, match="never override a risk block"):
            gate.authorize(
                make_proposal(passed=False),
                approval_text=smuggled,
                live_quote=live,
                allow_override=False,
            )

    def test_a_typed_override_still_works(self, config):
        """The human path is unchanged -- only the non-typed path is restricted."""
        gate = ApprovalGate(
            config,
            kill_switch=KillSwitch(config.kill_switch_path),
            audit=AuditLog(config.audit_path),
        )
        live = Quote("BTC-USD", Decimal("80000"), Decimal("80000"), Decimal("80000"), utcnow())
        auth = gate.authorize(
            make_proposal(passed=False),
            approval_text=f"execute abc123def456 {OVERRIDE_PHRASE}",
            live_quote=live,
            allow_override=True,
        )
        assert auth.overridden

    def test_a_decision_can_still_approve_a_passing_proposal(self, config):
        gate = ApprovalGate(
            config,
            kill_switch=KillSwitch(config.kill_switch_path),
            audit=AuditLog(config.audit_path),
        )
        live = Quote("BTC-USD", Decimal("80000"), Decimal("80000"), Decimal("80000"), utcnow())
        decision = Decision(
            proposal_id="abc123def456", kind=DecisionKind.ACCEPT, decided_at=utcnow()
        )
        auth = gate.authorize(
            make_proposal(passed=True),
            approval_text=decision.approval_text,
            live_quote=live,
            allow_override=False,
        )
        assert not auth.overridden

    def test_decision_approval_text_names_the_proposal(self):
        decision = Decision(
            proposal_id="abc123def456", kind=DecisionKind.ACCEPT, decided_at=utcnow()
        )
        assert "abc123def456" in decision.approval_text


class TestDecisionParsing:
    def test_valid_decision_round_trips(self):
        decision = decision_from_dict(
            {"proposal_id": "abc123def456", "decision": "decline", "note": "too wide a spread"}
        )
        assert decision.kind is DecisionKind.DECLINE
        assert decision.to_dict()["proposal_id"] == "abc123def456"

    @pytest.mark.parametrize(
        "payload",
        [
            {"decision": "accept"},
            {"proposal_id": "", "decision": "accept"},
            {"proposal_id": "../../etc/passwd", "decision": "accept"},
            {"proposal_id": "abc123def456", "decision": "maybe"},
            {"proposal_id": "abc123def456"},
        ],
    )
    def test_malformed_decisions_are_rejected(self, payload):
        with pytest.raises(AgentError):
            decision_from_dict(payload)

    def test_proposal_id_must_be_hex(self):
        """Stops arbitrary text reaching the gate's id matcher."""
        with pytest.raises(AgentError, match="not a valid proposal id"):
            decision_from_dict({"proposal_id": "abc; rm -rf /", "decision": "accept"})

    def test_control_characters_are_stripped(self):
        assert "\x00" not in sanitize_note("hello\x00world")

    def test_notes_are_length_bounded(self):
        assert len(sanitize_note("x" * 5000)) == 500


class TestOutcomeScoring:
    def anchor(self):
        return utcnow() - timedelta(hours=20)

    def test_a_buy_into_a_rally_is_a_win(self):
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([100 + i * 2 for i in range(20)], anchor=t0),
        )
        assert outcome.verdict is Verdict.WIN

    def test_a_buy_into_a_selloff_is_a_loss(self):
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([100 - i * 2 for i in range(20)], anchor=t0),
        )
        assert outcome.verdict is Verdict.LOSS

    def test_a_sell_is_scored_in_the_opposite_direction(self):
        """A sell proposal wins when the price falls."""
        t0 = self.anchor()
        falling = make_candles([100 - i * 2 for i in range(20)], anchor=t0)
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.SELL,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=falling,
        )
        assert outcome.verdict is Verdict.WIN
        assert outcome.signed_move_pct > 0

    def test_a_gain_that_does_not_cover_the_exit_is_a_loss(self):
        """The mark rose 0.8% past the ask paid: a win under the old 0.75%
        hurdle, but selling at the bid, 0.95% below the mark, loses money."""
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([100.8] * 20, anchor=t0),
        )
        assert outcome.verdict is Verdict.LOSS
        # 100.8 x (1 - 0.0095) = 99.8424: -0.1576% after the round trip.
        assert outcome.signed_move_pct == Decimal("-0.1576")
        assert "exit at the bid" in outcome.reason

    def test_a_profit_under_the_hurdle_is_flat(self):
        """Made money after the round trip, but no more than the hurdle."""
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([101.1] * 20, anchor=t0),
        )
        assert outcome.verdict is Verdict.FLAT
        assert Decimal("0") < outcome.signed_move_pct <= DEFAULT_HURDLE_PCT

    def test_a_sell_buys_back_at_the_ask(self):
        """A sell's round trip closes half a spread above the mark."""
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.SELL,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([99.5] * 20, anchor=t0),
            exit_cost_pct=Decimal("1"),
        )
        # Bought back at 99.5 x 1.01 = 100.495, above the 100 it sold at.
        assert outcome.verdict is Verdict.LOSS

    def test_an_unelapsed_horizon_is_pending_not_a_loss(self):
        """An agent that just started must not look like a bad one."""
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=utcnow(),
            candles=make_candles([100 + i for i in range(20)], anchor=t0),
        )
        assert outcome.verdict is Verdict.PENDING

    def test_too_few_bars_is_pending_and_says_how_many(self):
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([100, 101, 102], anchor=t0),
        )
        assert outcome.verdict is Verdict.PENDING
        assert "3 of 6 bars" in outcome.reason

    def test_other_symbols_do_not_resolve_a_proposal(self):
        t0 = self.anchor()
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=make_candles([100 + i * 5 for i in range(20)], symbol="ETH-USD", anchor=t0),
        )
        assert outcome.verdict is Verdict.PENDING

    def test_a_non_positive_reference_price_is_unscorable(self):
        outcome = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("0"),
            proposed_at=self.anchor(),
            candles=[],
        )
        assert outcome.verdict is Verdict.UNSCORABLE


class TestAccuracyStats:
    def outcomes(self):
        t0 = utcnow() - timedelta(hours=20)
        series = {
            "win": [100 + i * 2 for i in range(20)],
            "loss": [100 - i * 2 for i in range(20)],
            "flat": [101.1] * 20,
        }
        return [
            score_proposal(
                proposal_id=name,
                symbol="BTC-USD",
                side=Side.BUY,
                reference_price=Decimal("100"),
                proposed_at=t0,
                candles=make_candles(prices, anchor=t0),
                regime="trending" if name == "win" else "ranging",
            )
            for name, prices in series.items()
        ]

    def test_win_rate_excludes_flat_outcomes(self):
        stats = aggregate(self.outcomes())
        assert stats.wins == 1 and stats.losses == 1 and stats.flat == 1
        assert stats.win_rate == pytest.approx(0.5)  # 1 of 2 decided, not 1 of 3

    def test_no_evidence_means_unknown_not_zero(self):
        """0% would read as 'wrong every time'; the truth is 'we don't know'."""
        assert AccuracyStats().win_rate is None

    def test_pending_outcomes_are_not_counted_as_evidence(self):
        t0 = utcnow()
        pending = score_proposal(
            proposal_id="p",
            symbol="BTC-USD",
            side=Side.BUY,
            reference_price=Decimal("100"),
            proposed_at=t0,
            candles=[],
        )
        stats = aggregate([pending])
        assert stats.pending == 1
        assert stats.resolved == 0
        assert stats.win_rate is None

    def test_grouping(self):
        grouped = group_by(self.outcomes(), "regime")
        assert set(grouped) == {"trending", "ranging"}
        assert grouped["trending"].wins == 1

    def test_grouping_rejects_an_unknown_key(self):
        with pytest.raises(ValueError):
            group_by([], "nonsense")

    def test_stats_serialize_for_the_dashboard(self):
        payload = aggregate(self.outcomes()).to_dict()
        assert payload["win_rate"] == pytest.approx(0.5)
        assert payload["decided"] == 2


def test_a_record_is_charged_half_its_own_spread():
    t0 = utcnow() - timedelta(hours=20)
    record = {
        "proposal_id": "p",
        "symbol": "BTC-USD",
        "side": "buy",
        "reference_price": "100",
        "recorded_at": t0.isoformat(),
        "spread_pct": "3.0",
    }
    candles = make_candles([101.6] * 20, anchor=t0)
    outcome = outcome_from_proposal_record(record, candles)
    assert outcome.exit_cost_pct == Decimal("1.5")
    # 101.6 x 0.985 = 100.076: +0.076%, under the hurdle.
    assert outcome.verdict is Verdict.FLAT

    # Without a recorded spread, the default 0.95%: 101.6 x 0.9905 = 100.635.
    unrecorded = outcome_from_proposal_record(
        {k: v for k, v in record.items() if k != "spread_pct"}, candles
    )
    assert unrecorded.exit_cost_pct == DEFAULT_EXIT_COST_PCT
    assert unrecorded.verdict is Verdict.WIN


def test_a_time_exit_is_not_scored_as_a_prediction():
    t0 = utcnow() - timedelta(hours=20)
    record = {
        "proposal_id": "p",
        "symbol": "BTC-USD",
        "side": "sell",
        "reference_price": "100",
        "recorded_at": t0.isoformat(),
        "exit": "time",
    }
    assert outcome_from_proposal_record(record, make_candles([90] * 20, anchor=t0)) is None


def test_scoring_straight_from_an_audit_record(tmp_path):
    """The dashboard's accuracy panel is built from the audit log."""
    t0 = utcnow() - timedelta(hours=20)
    record = {
        "proposal_id": "abc123def456",
        "symbol": "BTC-USD",
        "side": "buy",
        "reference_price": "100",
        "recorded_at": t0.isoformat(),
        "regime": "trending",
        "score": 0.6,
        "confidence": 0.8,
    }
    outcome = outcome_from_proposal_record(
        record, make_candles([100 + i * 2 for i in range(20)], anchor=t0)
    )
    assert outcome.verdict is Verdict.WIN
    assert outcome.regime == "trending"


def test_malformed_audit_record_scores_to_none():
    assert outcome_from_proposal_record({"proposal_id": "x"}, []) is None
