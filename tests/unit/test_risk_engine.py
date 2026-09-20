from __future__ import annotations

from datetime import date

from robinhood_crypto_agent.audit.log import DailyActivity
from robinhood_crypto_agent.models import Regime, RiskCheckResult, Side, Signal, TradeProposal
from robinhood_crypto_agent.risk.engine import evaluate
from robinhood_crypto_agent.risk.limits import RiskLimits


def _risk_limits(**overrides) -> RiskLimits:
    kwargs = dict(
        max_position_usd=50.0,
        max_daily_loss_usd=100.0,
        max_daily_exposure_usd=250.0,
        max_open_positions=3,
        price_drift_tolerance_pct=0.5,
        allowed_symbols=["BTC", "ETH"],
    )
    kwargs.update(overrides)
    return RiskLimits(**kwargs)


def _proposal(symbol="BTC", side=Side.BUY, notional_usd=50.0) -> TradeProposal:
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
        risk_check=RiskCheckResult(passed=True),
    )


def _activity(**overrides) -> DailyActivity:
    kwargs = dict(
        date=date(2024, 1, 1),
        exposure_usd=0.0,
        realized_loss_usd=0.0,
        open_position_symbols=set(),
        kill_switch_engaged=False,
    )
    kwargs.update(overrides)
    return DailyActivity(**kwargs)


def test_passes_within_all_limits():
    result = evaluate(_proposal(notional_usd=50.0), _risk_limits(), _activity())
    assert result.passed
    assert result.violations == []


def test_rejects_symbol_not_on_watchlist():
    result = evaluate(_proposal(symbol="DOGE", notional_usd=10.0), _risk_limits(), _activity())
    assert not result.passed
    assert any("DOGE" in v for v in result.violations)


def test_rejects_oversized_position():
    result = evaluate(
        _proposal(notional_usd=51.0), _risk_limits(max_position_usd=50.0), _activity()
    )
    assert not result.passed
    assert any("max_position_usd" in v for v in result.violations)


def test_rejects_when_exposure_cap_would_be_exceeded():
    activity = _activity(exposure_usd=220.0)
    result = evaluate(
        _proposal(notional_usd=50.0), _risk_limits(max_daily_exposure_usd=250.0), activity
    )
    assert not result.passed
    assert any("max_daily_exposure_usd" in v for v in result.violations)


def test_rejects_when_daily_loss_cap_already_reached():
    activity = _activity(realized_loss_usd=100.0)
    result = evaluate(
        _proposal(notional_usd=10.0), _risk_limits(max_daily_loss_usd=100.0), activity
    )
    assert not result.passed
    assert any("max_daily_loss_usd" in v for v in result.violations)


def test_rejects_opening_beyond_max_open_positions():
    activity = _activity(open_position_symbols={"ETH"})
    limits = _risk_limits(max_open_positions=1, allowed_symbols=["BTC", "ETH"])
    result = evaluate(_proposal(symbol="BTC", side=Side.BUY, notional_usd=10.0), limits, activity)
    assert not result.passed
    assert any("max_open_positions" in v for v in result.violations)


def test_selling_an_open_position_does_not_count_against_max_open_positions():
    activity = _activity(open_position_symbols={"BTC", "ETH"})
    limits = _risk_limits(max_open_positions=2, allowed_symbols=["BTC", "ETH"])
    result = evaluate(_proposal(symbol="BTC", side=Side.SELL, notional_usd=10.0), limits, activity)
    assert result.passed


def test_buying_more_of_an_already_open_position_does_not_double_count():
    activity = _activity(open_position_symbols={"BTC"})
    limits = _risk_limits(max_open_positions=1, allowed_symbols=["BTC"])
    result = evaluate(_proposal(symbol="BTC", side=Side.BUY, notional_usd=10.0), limits, activity)
    assert result.passed


def test_rejects_when_kill_switch_engaged():
    activity = _activity(kill_switch_engaged=True)
    result = evaluate(_proposal(notional_usd=10.0), _risk_limits(), activity)
    assert not result.passed
    assert any("kill switch" in v for v in result.violations)


def test_collects_multiple_violations_at_once():
    activity = _activity(realized_loss_usd=100.0, kill_switch_engaged=True)
    result = evaluate(
        _proposal(symbol="DOGE", notional_usd=999.0),
        _risk_limits(max_position_usd=50.0),
        activity,
    )
    assert not result.passed
    assert len(result.violations) >= 3
