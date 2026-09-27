"""The ledger: the ladder's state, rebuilt from the audit log.

What these pin down: only the ladder's own orders count; lots close first in
first out; a cycle opens with its first buy and takes that buy's anchor, and
closes when a sell empties the position; an open order holds its step; an
order that ended unfilled holds nothing; and realized P&L comes from the
recorded notionals.
"""

from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.ledger import ladder_position, ladder_positions
from robinhood_crypto_agent.models import ExecutionRecord, Side, utcnow

D = Decimal


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def propose(audit, pid, *, side="buy", step=0, anchor="100", rule="dip", ladder=True):
    detail = {"rule": rule, "step": step, "anchor": anchor}
    audit.append(
        KIND_PROPOSAL,
        {
            "proposal_id": pid,
            "symbol": "BTC-USD",
            "side": side,
            **({"strategy": "ladder"} if ladder else {}),
            "proposal": {"sizing_detail": {"ladder": detail} if ladder else {}},
        },
    )


def fill(audit, pid, quantity, *, side="buy", price="100", state="filled"):
    quantity = D(quantity)
    audit.record_execution(
        ExecutionRecord(
            proposal_id=pid,
            symbol="BTC-USD",
            side=Side(side),
            recorded_at=utcnow(),
            requested_quantity=quantity,
            filled_quantity=quantity,
            notional=quantity * D(price),
            order_id=f"ord-{pid}",
            state=state,
        )
    )


def test_a_first_buy_opens_a_cycle_with_its_anchor(audit):
    propose(audit, "b0", anchor="120")
    fill(audit, "b0", "0.05", price="114")
    position = ladder_position(audit, "BTC-USD")
    assert position.held == D("0.05") and position.in_cycle
    assert position.anchor == D("120") and position.bought == {0}
    assert position.cost == D("5.70")


def test_sells_close_the_oldest_lots_first_and_book_the_pnl(audit):
    propose(audit, "b0", step=0)
    fill(audit, "b0", "0.1", price="95")
    propose(audit, "b1", step=1)
    fill(audit, "b1", "0.1", price="90")
    propose(audit, "s0", side="sell", step=0, rule="take_profit")
    fill(audit, "s0", "0.15", side="sell", price="110")
    position = ladder_position(audit, "BTC-USD")
    [lot] = position.lots
    assert lot.proposal_id == "b1" and lot.quantity == D("0.05")
    assert position.realized_pnl == D("0.1") * 15 + D("0.05") * 20  # 1.5 + 1.0
    assert position.sold == {0} and position.bought == {0, 1}


def test_a_sell_that_empties_the_position_closes_the_cycle(audit):
    propose(audit, "b0")
    fill(audit, "b0", "0.1")
    propose(audit, "x", side="sell", step=None, rule="trend_exit")
    fill(audit, "x", "0.1", side="sell", price="90")
    position = ladder_position(audit, "BTC-USD")
    assert position.held == 0 and not position.in_cycle
    assert position.bought == frozenset() and position.flat_since is not None
    assert position.realized_pnl == D("-1.0")


def test_an_open_order_holds_its_step_and_a_canceled_one_does_not(audit):
    propose(audit, "b0")
    fill(audit, "b0", "0", state="confirmed")
    position = ladder_position(audit, "BTC-USD")
    assert position.in_cycle and position.bought == {0} and position.held == 0
    fill(audit, "b0", "0", state="canceled")
    position = ladder_position(audit, "BTC-USD")
    assert not position.in_cycle and position.bought == frozenset()


def test_an_open_sell_is_reported(audit):
    propose(audit, "b0")
    fill(audit, "b0", "0.1")
    propose(audit, "x", side="sell", step=None, rule="trend_exit")
    fill(audit, "x", "0", side="sell", state="queued")
    assert ladder_position(audit, "BTC-USD").open_sell


def test_rejected_orders_move_nothing(audit):
    propose(audit, "b0")
    fill(audit, "b0", "0.1", state="rejected")
    position = ladder_position(audit, "BTC-USD")
    assert position.held == 0 and not position.in_cycle


def test_orders_that_are_not_the_ladders_are_ignored(audit):
    """System 1's buys, and anything bought outside the agent, are not the ladder's."""
    propose(audit, "legacy", ladder=False)
    fill(audit, "legacy", "1")
    fill(audit, "never-proposed", "1")
    assert ladder_positions(audit) == {}
    assert ladder_position(audit, "BTC-USD").held == 0


def test_a_fill_without_a_notional_leaves_cost_unknown(audit):
    propose(audit, "b0")
    fill(audit, "b0", "0.1", price="0")
    assert ladder_position(audit, "BTC-USD").cost is None
