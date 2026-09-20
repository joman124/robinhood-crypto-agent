from __future__ import annotations

import pytest

from robinhood_crypto_agent.config import AppConfig, ExecutionMode, RegimeConfig, StrategyWeights
from robinhood_crypto_agent.execution.adapter import (
    ExecutionNotAutomatedError,
    HumanApprovalRequiredAdapter,
    get_execution_adapter,
)
from robinhood_crypto_agent.models import Regime, RiskCheckResult, Side, Signal, TradeProposal
from robinhood_crypto_agent.risk.limits import RiskLimits


def _proposal() -> TradeProposal:
    signal = Signal(
        symbol="BTC", regime=Regime.TRENDING, composite_value=0.5, composite_confidence=0.8, side=Side.BUY
    )
    return TradeProposal(
        symbol="BTC",
        side=Side.BUY,
        quantity=0.5,
        reference_price=100.0,
        notional_usd=50.0,
        rationale="test",
        signal=signal,
        risk_check=RiskCheckResult(passed=True),
    )


def _config(execution_mode: ExecutionMode) -> AppConfig:
    return AppConfig(
        execution_mode=execution_mode,
        risk_limits=RiskLimits(
            max_position_usd=50.0,
            max_daily_loss_usd=100.0,
            max_daily_exposure_usd=250.0,
            max_open_positions=3,
            price_drift_tolerance_pct=0.5,
            allowed_symbols=["BTC"],
        ),
        watchlist=["BTC"],
        strategy_weights=StrategyWeights(
            regimes={"trending": {}, "ranging": {}},
            signal_threshold=0.3,
            min_confidence=0.3,
            regime=RegimeConfig(),
        ),
    )


def test_human_approval_required_adapter_always_raises():
    """The core safety property: this adapter must never be able to submit
    an order, under any input."""
    adapter = HumanApprovalRequiredAdapter()
    with pytest.raises(ExecutionNotAutomatedError):
        adapter.submit_order(_proposal())


def test_factory_returns_human_approval_adapter_in_propose_only_mode():
    adapter = get_execution_adapter(_config(ExecutionMode.PROPOSE_ONLY))
    assert isinstance(adapter, HumanApprovalRequiredAdapter)
    with pytest.raises(ExecutionNotAutomatedError):
        adapter.submit_order(_proposal())


def test_factory_raises_notimplemented_for_auto_mode():
    """AUTO mode has no adapter implementation yet - the factory must fail
    loudly rather than silently falling back to PROPOSE_ONLY behavior."""
    with pytest.raises(NotImplementedError):
        get_execution_adapter(_config(ExecutionMode.AUTO))
