"""The audit log both records and enforces -- daily caps are read from it."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from robinhood_crypto_agent.audit import AuditLog, day_from
from robinhood_crypto_agent.errors import AuditError
from robinhood_crypto_agent.models import ExecutionRecord, Side, utcnow


def execution(log, *, state="filled", notional="100", quantity="0.001", proposal="p1"):
    return log.record_execution(
        ExecutionRecord(
            proposal_id=proposal,
            symbol="BTC-USD",
            side=Side.BUY,
            recorded_at=utcnow(),
            requested_quantity=Decimal(quantity),
            filled_quantity=Decimal(quantity),
            notional=Decimal(notional),
            order_id="ord-1",
            state=state,
        )
    )


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def test_executed_notional_accumulates(log):
    execution(log, notional="100")
    execution(log, notional="50")
    assert log.daily_activity().executed_notional == Decimal("150")


@pytest.mark.parametrize("state", ["rejected", "failed", "voided"])
def test_failed_orders_do_not_consume_the_daily_budget(log, state):
    """A rejected order moved no money, so it must not eat the cap."""
    execution(log, state=state, notional="900")
    assert log.daily_activity().executed_notional == Decimal("0")
    assert log.daily_activity().execution_count == 0


def test_realized_pnl_is_superseded_not_summed(log):
    """Robinhood reports a running total; summing would double-count."""
    today = utcnow().date()
    log.record_pnl(today, Decimal("-30"), "get_realized_pnl")
    log.record_pnl(today, Decimal("-45"), "get_realized_pnl")
    activity = log.daily_activity()
    assert activity.realized_pnl == Decimal("-45")
    assert activity.realized_loss == Decimal("45")


def test_distinct_sources_are_summed(log):
    today = utcnow().date()
    log.record_pnl(today, Decimal("-30"), "get_realized_pnl")
    log.record_pnl(today, Decimal("-10"), "manual")
    assert log.daily_activity().realized_pnl == Decimal("-40")


def test_a_profitable_day_reports_zero_loss(log):
    log.record_pnl(utcnow().date(), Decimal("120"), "get_realized_pnl")
    assert log.daily_activity().realized_loss == Decimal("0")


def test_other_days_do_not_leak_into_today(log):
    log.record_pnl(utcnow().date() - timedelta(days=1), Decimal("-500"), "get_realized_pnl")
    assert log.daily_activity().realized_pnl == Decimal("0")


def test_partial_fills_accumulate_against_one_proposal(log):
    """A staged plan fills over tranches; that is accumulation, not duplication."""
    execution(log, quantity="0.4", proposal="staged")
    execution(log, quantity="0.3", proposal="staged")
    assert log.filled_quantity_for("staged") == Decimal("0.7")
    assert len(log.executions_for("staged")) == 2


def test_rejected_fills_do_not_count_toward_filled_quantity(log):
    execution(log, quantity="0.4", proposal="s", state="rejected")
    assert log.filled_quantity_for("s") == Decimal("0")


def test_remaining_notional_never_goes_negative(log):
    execution(log, notional="1500")
    assert log.daily_activity().remaining_notional(Decimal("1000")) == Decimal("0")


def test_hand_edited_log_is_rejected_loudly(log):
    execution(log)
    log.path.write_text(log.path.read_text() + "this is not json\n")
    with pytest.raises(AuditError, match="must not be hand-edited"):
        list(log.events())


def test_unknown_event_kinds_are_rejected(log):
    with pytest.raises(AuditError, match="unknown audit event kind"):
        log.append("whatever", {})


def test_missing_log_reads_as_empty(tmp_path):
    log = AuditLog(tmp_path / "nope.jsonl")
    assert list(log.events()) == []
    assert log.daily_activity().executed_notional == Decimal("0")
    assert log.find_proposal("x") is None


def test_day_from_accepts_several_forms():
    assert day_from("2026-03-01") == date(2026, 3, 1)
    assert day_from(None) == utcnow().date()
    assert day_from(utcnow()) == utcnow().date()
