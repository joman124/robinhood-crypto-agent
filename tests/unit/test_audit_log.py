from __future__ import annotations

from datetime import UTC, datetime

from robinhood_crypto_agent.audit.log import AuditLog
from robinhood_crypto_agent.execution.kill_switch import engage_kill_switch
from robinhood_crypto_agent.models import (
    ExecutionRecord,
    Regime,
    RiskCheckResult,
    Side,
    Signal,
    TradeProposal,
)


def _proposal(symbol="BTC", side=Side.BUY, notional_usd=50.0, passed=True) -> TradeProposal:
    signal = Signal(
        symbol=symbol,
        regime=Regime.TRENDING,
        composite_value=0.5,
        composite_confidence=0.8,
        side=side,
    )
    return TradeProposal(
        symbol=symbol,
        side=side,
        quantity=notional_usd / 100.0,
        reference_price=100.0,
        notional_usd=notional_usd,
        rationale="test",
        signal=signal,
        risk_check=RiskCheckResult(passed=passed),
    )


def test_log_and_read_round_trip(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    proposal = _proposal()
    log.log_proposal(proposal)

    entries = log.read_entries()
    assert len(entries) == 1
    assert entries[0]["type"] == "proposal"
    assert entries[0]["proposal"]["id"] == proposal.id


def test_find_proposal_by_id(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    proposal = _proposal()
    log.log_proposal(proposal)

    found = log.find_proposal(proposal.id)
    assert found is not None
    assert found["symbol"] == "BTC"
    assert log.find_proposal("nonexistent") is None


def test_daily_activity_aggregates_todays_executions(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    proposal = _proposal(symbol="BTC", notional_usd=50.0)
    log.log_proposal(proposal)

    today = datetime.now(UTC)
    log.log_execution(
        ExecutionRecord(
            proposal_id=proposal.id,
            symbol="BTC",
            side=Side.BUY,
            status="filled",
            filled_price=100.0,
            filled_qty=0.5,
            timestamp=today,
        )
    )

    activity = log.read_daily_activity(today.date())
    assert activity.exposure_usd == 50.0
    assert "BTC" in activity.open_position_symbols
    assert activity.realized_loss_usd == 0.0


def test_daily_activity_tracks_realized_loss(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    today = datetime.now(UTC)

    log.log_execution(
        ExecutionRecord(
            proposal_id="p1",
            symbol="BTC",
            side=Side.SELL,
            status="filled",
            filled_price=90.0,
            filled_qty=1.0,
            realized_pnl_usd=-10.0,
            timestamp=today,
        )
    )

    activity = log.read_daily_activity(today.date())
    assert activity.realized_loss_usd == 10.0


def test_positive_realized_pnl_does_not_count_as_a_loss(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    today = datetime.now(UTC)

    log.log_execution(
        ExecutionRecord(
            proposal_id="p1",
            symbol="BTC",
            side=Side.SELL,
            status="filled",
            filled_price=110.0,
            filled_qty=1.0,
            realized_pnl_usd=10.0,
            timestamp=today,
        )
    )

    activity = log.read_daily_activity(today.date())
    assert activity.realized_loss_usd == 0.0


def test_open_position_tracked_across_days_not_just_today(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    yesterday = datetime(2024, 1, 1, tzinfo=UTC)
    today = datetime(2024, 1, 2, tzinfo=UTC)

    log.log_execution(
        ExecutionRecord(
            proposal_id="p1",
            symbol="BTC",
            side=Side.BUY,
            status="filled",
            filled_price=100.0,
            filled_qty=1.0,
            timestamp=yesterday,
        )
    )

    activity = log.read_daily_activity(today.date())
    assert "BTC" in activity.open_position_symbols
    assert activity.exposure_usd == 0.0  # nothing executed *today*


def test_sell_closes_open_position(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    day1 = datetime(2024, 1, 1, tzinfo=UTC)
    day2 = datetime(2024, 1, 2, tzinfo=UTC)

    log.log_execution(
        ExecutionRecord(
            proposal_id="p1",
            symbol="BTC",
            side=Side.BUY,
            status="filled",
            filled_price=100.0,
            filled_qty=1.0,
            timestamp=day1,
        )
    )
    log.log_execution(
        ExecutionRecord(
            proposal_id="p2",
            symbol="BTC",
            side=Side.SELL,
            status="filled",
            filled_price=110.0,
            filled_qty=1.0,
            timestamp=day2,
        )
    )

    activity = log.read_daily_activity(day2.date())
    assert "BTC" not in activity.open_position_symbols


def test_unfilled_executions_are_ignored(tmp_path):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    today = datetime.now(UTC)
    log.log_execution(
        ExecutionRecord(
            proposal_id="p1",
            symbol="BTC",
            side=Side.BUY,
            status="rejected",
            timestamp=today,
        )
    )
    activity = log.read_daily_activity(today.date())
    assert activity.exposure_usd == 0.0
    assert activity.open_position_symbols == set()


def test_kill_switch_state_reflected(tmp_path, monkeypatch):
    log = AuditLog(tmp_path / "audit_log.jsonl")
    kill_switch_path = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(
        "robinhood_crypto_agent.audit.log.is_kill_switch_engaged",
        lambda: kill_switch_path.exists(),
    )

    assert not log.read_daily_activity().kill_switch_engaged
    engage_kill_switch(kill_switch_path)
    assert log.read_daily_activity().kill_switch_engaged
