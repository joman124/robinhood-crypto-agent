"""The order desk: an accepted proposal becomes one real order, followed to its end.

Robinhood is faked; the proposal is a real split proposal, logged by a real
loop cycle. What these pin down: one accept is one order, ever; every way an
accept can be refused sends nothing; a lost answer is found by its id rather
than sent again; and fills are recorded as deltas the ledger can sum.
"""

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_EXECUTION, KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.decisions import Decision, DecisionKind
from robinhood_crypto_agent.errors import AgentError, ApprovalError, ConfigError, ContractViolation
from robinhood_crypto_agent.execution import desk as desk_mod
from robinhood_crypto_agent.execution.desk import LOOKUP_GRACE, MAX_DECISION_AGE, OrderDesk
from robinhood_crypto_agent.execution.gate import ApprovalGate
from robinhood_crypto_agent.execution.kill_switch import KillSwitch
from robinhood_crypto_agent.execution.orders import api_order_body, client_order_id
from robinhood_crypto_agent.ledger import split_book
from robinhood_crypto_agent.models import OrderType, Proposal, utcnow
from robinhood_crypto_agent.net import HttpError
from robinhood_crypto_agent.numeric import round_money
from robinhood_crypto_agent.robinhood import ORDERS_PATH
from robinhood_crypto_agent.serde import proposal_from_dict
from tests.unit import test_runner
from tests.unit.test_robinhood_client import PUBLIC, client
from tests.unit.test_runner import FakeRobinhood, make_runner

config = test_runner.config  # the same AgentConfig fixture

ORDER_ID = "497f6eca-6276-4993-bfeb-53cbbbba6f08"


def row(body, state, filled="0", price=None):
    """An order as Robinhood's API returns it."""
    return {
        "id": ORDER_ID,
        "client_order_id": body["client_order_id"],
        "symbol": body["symbol"],
        "side": body["side"],
        "type": body["type"],
        "state": state,
        "average_price": price,
        "filled_asset_quantity": filled,
        "executions": [],
    }


class FakeBroker(FakeRobinhood):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.error = None
        self.created = True  # whether a failed send still created the order
        self.order = None

    def place_order(self, body):
        self.sent.append(body)
        if self.error is None or self.created:
            self.order = row(body, "open")
        if self.error is not None:
            raise self.error
        return self.order

    def get_order(self, order_id):
        assert order_id == ORDER_ID
        return self.order

    def find_order(self, symbol, client_order_id, *, since):
        assert since <= utcnow()
        order = self.order
        return order if order and order["client_order_id"] == client_order_id else None


@pytest.fixture
def proposed(config):
    make_runner(config).cycle(force=True)
    [record] = AuditLog(config.audit_path).events(kind=KIND_PROPOSAL)
    assert record["status"] == "proposed" and record["side"] == "buy"
    return record


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def audit(config):
    return AuditLog(config.audit_path)


def desk(config, broker):
    return OrderDesk(config, AuditLog(config.audit_path), broker)


def accept(proposal_id, *, ago=timedelta(0), kind=DecisionKind.ACCEPT):
    return Decision(proposal_id, kind, utcnow() - ago)


def states(audit, proposal_id):
    return [r["state"] for r in audit.executions_for(proposal_id)]


def relog(audit, proposed, **changes):
    """Log the proposal again with ``changes``: find_proposal reads the latest."""
    fields = {k: v for k, v in proposed.items() if k not in ("kind", "recorded_at")}
    audit.append(KIND_PROPOSAL, {**fields, **changes})


class TestSending:
    def test_an_accept_sends_one_limit_order_and_records_it(self, config, proposed, broker, audit):
        pid = proposed["proposal_id"]
        [event] = desk(config, broker).act([accept(pid)])

        [body] = broker.sent
        assert body["client_order_id"] == client_order_id(pid)
        assert (body["side"], body["type"], body["symbol"]) == ("buy", "limit", "BTC-USD")
        assert body["limit_order_config"]["asset_quantity"] == proposed["quantity"]
        assert body["limit_order_config"]["time_in_force"] == "gtc"
        assert event.title == "Order placed"
        assert states(audit, pid) == ["submitting", "open"]

    def test_one_accept_is_one_order_ever_even_across_a_restart(self, config, proposed, broker):
        decisions = [accept(proposed["proposal_id"])]
        desk(config, broker).act(decisions)
        assert desk(config, broker).act(decisions) == []  # a restart: already handled
        assert len(broker.sent) == 1

    def test_a_second_accept_of_a_sent_proposal_sends_nothing(self, config, proposed, broker):
        pid = proposed["proposal_id"]
        desk(config, broker).act([accept(pid, ago=timedelta(minutes=2))])
        [event] = desk(config, broker).act([accept(pid)])
        assert len(broker.sent) == 1
        assert "already sent" in event.message

    def test_the_latest_decision_wins_so_a_decline_after_an_accept_sends_nothing(
        self, config, proposed, broker
    ):
        pid = proposed["proposal_id"]
        decisions = [accept(pid, ago=timedelta(minutes=1)), accept(pid, kind=DecisionKind.DECLINE)]
        assert desk(config, broker).act(decisions) == []
        assert broker.sent == []


class TestRefusals:
    """Each sends nothing, says why, and is noted so it is said once."""

    def refused(self, config, broker, audit, decision):
        [event] = desk(config, broker).act([decision])
        assert broker.sent == []
        assert event.title == "Accept not sent"
        assert audit.executions_for(decision.proposal_id) == []
        assert desk(config, broker).act([decision]) == []  # noted: not said twice
        return event.message

    def test_a_stale_accept(self, config, proposed, broker, audit):
        late = accept(proposed["proposal_id"], ago=MAX_DECISION_AGE + timedelta(minutes=1))
        assert "Accept the current proposal again" in self.refused(config, broker, audit, late)

    def test_an_engaged_kill_switch(self, config, proposed, broker, audit):
        KillSwitch(config.kill_switch_path).engage("test")
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert "kill switch" in message.lower()

    def test_a_price_that_moved_past_the_drift_tolerance(self, config, proposed, broker, audit):
        broker.mark *= Decimal("1.02")
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert "price has moved" in message

    def test_a_risk_blocked_proposal_whatever_the_button_says(self, config, proposed, broker, audit):
        relog(audit, proposed, status="rejected_by_risk")
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert "passed every risk rule" in message

    def test_an_older_proposal_for_the_same_trade(self, config, proposed, broker, audit):
        """The next hour re-logs the trade under a new id; accepting the old
        one after it must never become a second order for the same trade."""
        relog(audit, proposed, proposal_id="f" * 12)
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert f"superseded by {'f' * 12}" in message

    def test_a_newer_proposal_while_the_first_order_is_working(
        self, config, proposed, broker, audit
    ):
        """The reviewer's double order: two ids for one trade, both accepted."""
        desk(config, broker).act([accept(proposed["proposal_id"])])
        newer = "f" * 12
        relog(audit, proposed, proposal_id=newer,
              proposal={**proposed["proposal"], "proposal_id": newer})
        [event] = desk(config, broker).act([accept(newer)])
        assert "is working" in event.message
        assert len(broker.sent) == 1

    def test_a_sell_beyond_what_the_short_term_sleeve_holds(self, config, proposed, broker, audit):
        """Never the long-term sleeve's coins, never the owner's own."""
        relog(audit, proposed, side="sell",
              proposal={**proposed["proposal"], "side": "sell"})
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert "less than this sell" in message

    def test_a_proposal_the_split_has_stopped_re_logging(
        self, config, proposed, broker, audit, monkeypatch
    ):
        later = utcnow() + timedelta(hours=2)
        monkeypatch.setattr(desk_mod, "utcnow", lambda: later)
        decision = Decision(proposed["proposal_id"], DecisionKind.ACCEPT, later)
        message = self.refused(config, broker, audit, decision)
        assert "over an hour old" in message

    def test_the_daily_cap_counts_orders_still_working(self, config, broker):
        cap = config.risk.max_daily_notional_usd
        with pytest.raises(AgentError, match="traded or working"):
            desk(config, broker)._check_daily_notional(Decimal("10"), cap - Decimal("5"))

    def test_the_gate_refuses_a_second_order_while_the_first_is_working(
        self, config, proposed, broker, audit
    ):
        """The MCP path (`rhca approve`) must not double what the desk sent."""
        pid = proposed["proposal_id"]
        desk(config, broker).act([accept(pid)])
        [quote] = broker.best_bid_ask(["BTC-USD"])
        with pytest.raises(ApprovalError, match="still working"):
            ApprovalGate(config, audit=audit).clear(
                proposal_from_dict(proposed["proposal"]),
                approval_text=f"accept {pid}",
                live_quote=quote,
            )

    def test_a_proposal_that_is_not_the_splits(self, config, proposed, broker, audit):
        relog(audit, proposed, strategy=None)
        self.refused(config, broker, audit, accept(proposed["proposal_id"]))

    def test_a_buy_past_the_daily_cap_after_another_order_filled(
        self, config, proposed, broker, audit
    ):
        cap = config.risk.max_daily_notional_usd
        audit.append(
            KIND_EXECUTION,
            {"proposal_id": "0" * 12, "symbol": "ETH-USD", "state": "filled",
             "filled_quantity": "1", "notional": str(cap)},
        )
        message = self.refused(config, broker, audit, accept(proposed["proposal_id"]))
        assert "daily cap" in message

    def test_an_old_stale_accept_is_noted_without_a_push(self, config, proposed, broker, audit):
        ancient = accept(proposed["proposal_id"], ago=timedelta(days=30))
        assert desk(config, broker).act([ancient]) == []
        assert broker.sent == []
        assert any(n.get("order_desk") for n in audit.events(kind="note"))


class TestFollowing:
    def test_fills_are_recorded_as_deltas_the_ledger_sums(self, config, proposed, broker, audit):
        pid = proposed["proposal_id"]
        desk(config, broker).act([accept(pid)])
        body = broker.sent[0]
        quantity = Decimal(proposed["quantity"])
        half = quantity / 2

        broker.order = row(body, "partially_filled", str(half), "50000")
        [event] = desk(config, broker).follow()
        assert event.title == "Order partially filled"

        broker.order = row(body, "filled", str(quantity), "50000")
        [event] = desk(config, broker).follow()
        assert event.title == "Order filled"
        assert desk(config, broker).follow() == []  # ended: no longer read

        assert states(audit, pid) == ["submitting", "open", "partially_filled", "filled"]
        assert audit.filled_quantity_for(pid) == quantity
        assert audit.daily_activity().executed_notional == round_money(Decimal("50000") * quantity)
        assert audit.daily_activity().execution_count == 1
        holding = split_book(
            audit, long_capital=Decimal("250"), short_capital=Decimal("250")
        ).short.holding("BTC-USD")
        assert holding.quantity == quantity and not holding.open_buy

    def test_fills_reported_on_a_failed_order_stay_on_the_books(
        self, config, proposed, broker, audit
    ):
        pid = proposed["proposal_id"]
        desk(config, broker).act([accept(pid)])
        half = Decimal(proposed["quantity"]) / 2
        broker.order = row(broker.sent[0], "failed", str(half), "50000")
        desk(config, broker).follow()
        assert states(audit, pid)[-2:] == ["partially_filled", "failed"]
        assert audit.filled_quantity_for(pid) == half
        assert desk(config, broker).follow() == []

    def test_an_unchanged_order_records_nothing(self, config, proposed, broker, audit):
        desk(config, broker).act([accept(proposed["proposal_id"])])
        assert desk(config, broker).follow() == []
        assert states(audit, proposed["proposal_id"]) == ["submitting", "open"]

    def test_a_lost_answer_is_found_by_its_id_not_sent_again(
        self, config, proposed, broker, audit
    ):
        pid = proposed["proposal_id"]
        broker.error = HttpError("POST orders failed: timed out")  # status None: unclear
        [event] = desk(config, broker).act([accept(pid)])
        assert event.title == "Order sent, no answer yet"
        assert states(audit, pid) == ["submitting"]

        desk(config, broker).follow()
        assert states(audit, pid) == ["submitting", "open"]
        assert len(broker.sent) == 1

    def test_a_lost_answer_with_no_order_fails_after_the_grace(
        self, config, proposed, broker, audit, monkeypatch
    ):
        pid = proposed["proposal_id"]
        broker.error, broker.created = HttpError("POST orders failed: timed out"), False
        desk(config, broker).act([accept(pid)])
        assert desk(config, broker).follow() == []  # still within the grace
        assert states(audit, pid) == ["submitting"]

        later = utcnow() + LOOKUP_GRACE + timedelta(minutes=1)
        monkeypatch.setattr(desk_mod, "utcnow", lambda: later)
        [event] = desk(config, broker).follow()
        assert event.title == "Order not placed"
        assert states(audit, pid) == ["submitting", "failed"]
        book = split_book(audit, long_capital=Decimal("250"), short_capital=Decimal("250"))
        assert not book.short.holding("BTC-USD").open_buy  # its cash is released

    def test_a_refusal_from_robinhood_is_recorded_failed(self, config, proposed, broker, audit):
        pid = proposed["proposal_id"]
        broker.error, broker.created = HttpError("HTTP 403 permission denied", 403), False
        [event] = desk(config, broker).act([accept(pid)])
        assert event.title == "Order refused by Robinhood"
        assert states(audit, pid) == ["submitting", "failed"]
        assert desk(config, broker).follow() == []


def with_order_type(proposed, order_type) -> Proposal:
    proposal = proposal_from_dict(proposed["proposal"])
    [tranche] = proposal.plan.tranches
    plan = replace(proposal.plan, tranches=[replace(tranche, order_type=order_type)])
    return replace(proposal, plan=plan)


def test_the_loop_sends_a_dashboard_accept_and_pushes_it(config, broker, monkeypatch):
    """End to end through the runner: a cycle logs the proposal, the owner
    accepts it, and the next cycle sends it, records it and pushes it."""
    from robinhood_crypto_agent import runner as runner_mod

    runner = make_runner(config, robinhood=broker, dashboard=("https://dash", "t"),
                         push_webhook="https://ntfy.sh/x")
    accepted = []
    monkeypatch.setattr(runner_mod.dashboard_mod, "fetch_decisions", lambda **_: accepted)
    monkeypatch.setattr(runner_mod.dashboard_mod, "sync", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(runner_mod.push_mod, "notify", lambda url, t, m: pushed.append(t) or True)

    runner.cycle(force=True)
    assert broker.sent == []  # proposed, not accepted
    [record] = AuditLog(config.audit_path).events(kind=KIND_PROPOSAL)
    accepted.append(accept(record["proposal_id"]))
    runner.cycle(force=True)

    assert len(broker.sent) == 1
    assert "Order placed" in pushed
    runner.cycle(force=True)
    assert len(broker.sent) == 1  # one accept, one order


class TestOrderBody:
    def test_the_id_is_derived_so_a_retry_names_the_same_order(self, proposed):
        proposal = proposal_from_dict(proposed["proposal"])
        assert api_order_body(proposal) == api_order_body(proposal)
        assert client_order_id("abc") != client_order_id("abc", 1)

    def test_a_market_only_pair_sends_a_market_order(self, proposed):
        body = api_order_body(with_order_type(proposed, OrderType.MARKET))
        assert body["market_order_config"] == {"asset_quantity": proposed["quantity"]}

    def test_stop_orders_are_never_sent_through_the_api(self, proposed):
        with pytest.raises(ContractViolation):
            api_order_body(with_order_type(proposed, OrderType.STOP_LOSS))


class TestClient:
    def test_an_order_is_signed_over_the_exact_body_it_sends(self):
        rh, http = client({"id": ORDER_ID})
        sent = {}
        inner = rh._http

        def capture(method, url, *, headers=None, body=None, timeout=None):
            sent["body"] = body
            return inner(method, url, headers=headers, body=body, timeout=timeout)

        rh._http = capture
        body = {"client_order_id": client_order_id("abc"), "side": "buy", "symbol": "BTC-USD"}
        rh.place_order(body)

        method, url, headers = http.calls[0]
        assert (method, url) == ("POST", "https://trading.robinhood.com" + ORDERS_PATH)
        # net.request_json sends json.dumps(body): exactly what was signed.
        signed = f"rh-key{headers['x-timestamp']}{ORDERS_PATH}POST{json.dumps(sent['body'])}"
        PUBLIC.verify(base64.b64decode(headers["x-signature"]), signed.encode())

    def test_an_order_id_must_be_a_uuid_before_it_goes_into_a_signed_path(self):
        rh, _ = client()
        with pytest.raises(ConfigError):
            rh.get_order("../accounts")

    def test_find_order_matches_the_client_order_id(self):
        cid = client_order_id("abc")
        rh, http = client({"results": [{"client_order_id": client_order_id("x")},
                                       {"client_order_id": cid, "id": ORDER_ID}]})
        since = datetime(2026, 10, 20, 7, 0, 5, tzinfo=timezone.utc)
        assert rh.find_order("BTC-USD", cid, since=since)["id"] == ORDER_ID
        assert http.calls[0][1].endswith(
            ORDERS_PATH + "?symbol=BTC-USD&created_at_start=2026-10-20T07:00:05Z"
        )
