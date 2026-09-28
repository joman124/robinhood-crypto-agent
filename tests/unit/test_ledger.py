"""The split's ledger: both sleeves, rebuilt from the audit log.

What these pin down: only the split's recorded fills count; each sleeve's cash
is its capital less what it spent plus what it got back, with an open buy's
dollars held out; a short-term position remembers the close it was entered
on until it is sold out; a tranche counts once it filled or while it is still
open; and realized P&L is first in, first out.
"""

from datetime import datetime, timezone
from decimal import Decimal

from robinhood_crypto_agent.audit import KIND_PROPOSAL, AuditLog
from robinhood_crypto_agent.ledger import (
    LONG_TERM,
    RULE_ENTRY,
    RULE_STOP,
    RULE_TRANCHE,
    SHORT_TERM,
    split_book,
)
from robinhood_crypto_agent.models import ExecutionRecord, Side, utcnow

D = Decimal
DAY1 = datetime(2026, 5, 1, tzinfo=timezone.utc)
DAY2 = datetime(2026, 5, 9, tzinfo=timezone.utc)


def propose(audit, pid, *, sleeve, rule, day, side="buy", symbol="BTC-USD", notional="25",
            price="50000", strategy="split"):
    audit.append(
        KIND_PROPOSAL,
        {
            "proposal_id": pid,
            "symbol": symbol,
            "side": side,
            "notional": notional,
            "reference_price": price,
            "strategy": strategy,
            "proposal": {
                "sizing_detail": {
                    strategy: {"sleeve": sleeve, "rule": rule, "day": day.isoformat()}
                }
            },
        },
    )


def execute(audit, pid, *, filled, notional, state="filled", side=Side.BUY, symbol="BTC-USD"):
    audit.record_execution(
        ExecutionRecord(
            proposal_id=pid, symbol=symbol, side=side, recorded_at=utcnow(),
            requested_quantity=D(filled), filled_quantity=D(filled), notional=D(notional),
            order_id=f"ord-{pid}", state=state,
        )
    )


def book(audit):
    return split_book(audit, long_capital=D(250), short_capital=D(250))


def test_an_empty_log_is_two_sleeves_of_cash(tmp_path):
    split = book(AuditLog(tmp_path / "audit.jsonl"))
    assert (split.long.cash, split.short.cash) == (D(250), D(250))
    assert split.long.holdings == split.short.holdings == {}


def test_an_entry_then_its_stop(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "in", sleeve=SHORT_TERM, rule=RULE_ENTRY, day=DAY1)
    execute(audit, "in", filled="0.0005", notional="25")
    held = book(audit).short.holdings["BTC-USD"]
    assert (held.quantity, held.cost, held.entry_day) == (D("0.0005"), D(25), DAY1)
    assert book(audit).short.cash == D(225)

    propose(audit, "out", sleeve=SHORT_TERM, rule=RULE_STOP, day=DAY2, side="sell")
    execute(audit, "out", filled="0.0005", notional="30", side=Side.SELL)
    split = book(audit)
    sold = split.short.holdings["BTC-USD"]
    assert not sold.held and sold.entry_day is None and sold.cost == 0
    assert sold.realized_pnl == D(5) and split.short.realized_pnl == D(5)
    assert split.short.cash == D(255)


def test_an_open_buy_holds_its_place_and_its_dollars(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "in", sleeve=SHORT_TERM, rule=RULE_ENTRY, day=DAY1, notional="25")
    execute(audit, "in", filled="0", notional="0", state="confirmed")
    split = book(audit)
    assert split.short.holdings["BTC-USD"].open_buy
    assert split.short.cash == D(225)  # committed while open

    execute(audit, "in", filled="0", notional="0", state="canceled")
    split = book(audit)
    assert not split.short.holdings["BTC-USD"].open_buy
    assert split.short.cash == D(250)


def test_tranches_count_when_filled_or_open_but_not_when_dead(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "t1", sleeve=LONG_TERM, rule=RULE_TRANCHE, day=DAY1, notional="12.5")
    execute(audit, "t1", filled="0.00025", notional="12.5")
    propose(audit, "t2", sleeve=LONG_TERM, rule=RULE_TRANCHE, day=DAY2, notional="12.5")
    execute(audit, "t2", filled="0", notional="0", state="queued")
    propose(audit, "t3", sleeve=LONG_TERM, rule=RULE_TRANCHE, day=DAY2, notional="12.5")
    execute(audit, "t3", filled="0", notional="0", state="rejected")
    held = book(audit).long.holdings["BTC-USD"]
    assert held.tranches == 2 and held.last_buy_day == DAY2
    assert held.open_buy and held.quantity == D("0.00025")
    assert book(audit).long.cash == D(225)  # $12.50 spent, $12.50 committed


def test_only_the_splits_orders_count(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "ladder", sleeve=SHORT_TERM, rule=RULE_ENTRY, day=DAY1, strategy="ladder")
    execute(audit, "ladder", filled="1", notional="50000")
    execute(audit, "unknown", filled="1", notional="50000")
    split = book(audit)
    assert split.short.holdings == {} and split.short.cash == D(250)


def test_a_fill_without_its_dollars_is_valued_at_the_proposed_price(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "in", sleeve=SHORT_TERM, rule=RULE_ENTRY, day=DAY1, price="40000")
    execute(audit, "in", filled="0.0005", notional="0")
    assert book(audit).short.holdings["BTC-USD"].cost == D(20)


def test_partial_fills_add_up(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    propose(audit, "in", sleeve=SHORT_TERM, rule=RULE_ENTRY, day=DAY1, notional="25")
    execute(audit, "in", filled="0.0002", notional="10", state="partially_filled")
    execute(audit, "in", filled="0.0003", notional="15", state="filled")
    held = book(audit).short.holdings["BTC-USD"]
    assert held.quantity == D("0.0005") and not held.open_buy
    assert book(audit).short.cash == D(225)
